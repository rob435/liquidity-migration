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
event rank middle-band skip: disabled
coin day-return gate: daily_return_1d >= 0%
idiosyncratic move gate: daily_return_1d - market_median_return_1d >= +8%
regime gate: market_pct_up_1d <= 0.55 OR coin daily_return_1d >= +20%
entry delay: 1 hour after daily signal close
max hold: 3 days
stop: 12% fixed
take profit: 20% fixed
gross exposure: 1.25
max active symbols: 6
symbol cooldown: 5 days
stop-pressure throttle: pause after 12 realized stops inside 14 days
cost multiplier: 3x base round-trip cost
```

The strategy is short-only because the best full-PIT evidence is in reversal after liquidity migration. Long continuation is not promoted unless a fresh full-PIT run clears costs, splits, drawdown, and report gates.

## Reference Evidence

Promoted full-PIT report after the hold/exit frontier confirmation:

```text
data/research_reports/research_20260516_promoted_default_after_patch
```

Full-PIT result on `2023-05-03` to `2026-05-03`:

```text
trades: 516
total return: +1218.79%
max drawdown: -14.54%
max no-new-high stretch: 90 days
worst 90d return: -5.89%
worst split return: +75.64%
average split Sharpe-like: 2.67
train return: +75.64%
validation return: +254.58%
OOS return: +111.75%
default chart: volume_event_best_equity_btc.png with BTC overlay and monthly/growth gridlines
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
- full trade ledger, basket ledger, equity curve, BTC overlay chart, monthly returns, JSON config, and Markdown report

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

The runner loops every `INTERVAL_SECONDS=300` by default. Each cycle:

1. Pulls the current Bybit USDT perpetual universe through rank 220 so the selected rank 31-150 strategy can be evaluated forward.
2. Excludes only stable/peg perps, including failed peg remnants such as USTCUSDT, before ranks/features are built.
3. Rebuilds recent 1h volume features from a 45-day lookback, using the forward-demo kline cache so normal cycles fetch only missing/new 1h bars instead of the whole window.
4. Exits existing demo positions first on fixed-stop/take-profit reconciliation, event decay, rank exit, or 3-day max hold.
5. Enters accepted liquidity-migration events after the 1-hour signal delay, subject to max-active, cooldown, stop-pressure, positive-day-return, residual-return, and stale-entry gates. Stale entries are skipped after 15 minutes by default so demo fills stay close to the backtest entry timestamp.
6. Sizes each accepted coin from the same weight used by the backtest: `gross_exposure / max_active_symbols`, currently `1.25 / 6 = 20.83%` of current Bybit demo USDT equity. `--max-order-notional-pct-equity` is only an explicit override. The continuous runner defaults entry leverage to 2x so the 125% gross target can be submitted without changing notional sizing.
7. Attaches exchange-native stop/take-profit to entry orders, then recomputes the ledger stop/take-profit from confirmed fill price. If the confirmed fill moves the rounded protection levels, the runner immediately updates Bybit trading-stop state and records the update status.
8. Sends Telegram only for material events when enabled: entries, exits, failed entry stop updates, position reconciliation, or position-report errors. Quiet cycles still write local reports but do not notify.
9. Writes expected/submitted order state into `event_demo_orders`, trade state into `event_demo_trades`, cycle telemetry into `event_demo_cycles`, and Markdown/JSON reports under `reports/event-demo`.

Order submission is still fail-closed: `--submit-orders` requires `--confirm-demo-orders`, `BYBIT_DEMO_API_KEY`, and `BYBIT_DEMO_API_SECRET`. Without those, the command is a dry-run scan.

Telegram may notify on material events, but it must not approve or submit orders. The continuous runner still fails startup when `TELEGRAM_ENABLED=1` but Telegram or Bybit demo credentials are missing, because event alerts include position/PnL context.

The exit-only risk watchdog is separate from the alpha loop:

```bash
SUBMIT_ORDERS=1 CONFIRM_DEMO_ORDERS=1 TELEGRAM_ENABLED=1 bash scripts/run_bybit_demo_ws_risk_engine.sh
```

It runs `event-risk-ws` by default. The priority order is:

1. Exchange-native stop/take-profit attached to the position.
2. WebSocket streams for live state: demo private position/order/execution streams plus the mainnet public ticker stream.
3. REST only as the demo fallback for order submission and periodic reconciliation.

This loop never opens positions and never approves orders through Telegram. It reads the demo trade ledger plus current Bybit positions, repairs missing or mismatched exchange-native stop/take-profit settings, subscribes to active-position tickers, and sends reduce-only exits when a live streamed mark price crosses the ledger stop, streamed mark crosses the take-profit, or the max-hold timestamp has passed. WebSocket execution messages can close the ledger without REST polling. Quiet cycles update the latest local report but do not spam system logs; startup and material risk events also keep timestamped JSON/Markdown audit copies under `reports/event-risk-ws`.

Bybit's current demo docs state that demo WebSocket supports private streams only, public data is the same as mainnet public WebSocket data, and WebSocket Trade order entry does not support demo trading. For that reason the demo VPS uses `ORDER_SUBMIT_MODE=ws_then_rest`: the risk decision path is WebSocket-first, but actual demo reduce-only order submission falls back to REST when the demo WS trade socket is unavailable. The demo VPS also uses the normal private execution stream because `execution.fast` is not accepted by the demo private socket. Do not claim demo WS order-entry or fast-execution evidence unless these limitations are retested and cleared.

WebSocket startup is bounded by `STREAM_START_TIMEOUT_SECONDS` / `--stream-start-timeout-seconds`, default 3 seconds per startup operation. If private or public socket startup blocks, the risk daemon records the timeout, writes reports, and keeps REST reconciliation plus exchange-native stops active instead of hanging before the watchdog loop starts.

Cycle lock files record the owning PID and are recovered when that PID is no longer alive, including locks configured with no age-based stale timeout. This is required so a killed probe or service process cannot permanently block the risk daemon after restart.

`EXIT_ORDER_MODE=market` is the default and fastest emergency exit. `EXIT_ORDER_MODE=limit_chase` uses bounded reduce-only IOC limits, controlled by `LIMIT_CHASE_ATTEMPTS`, `LIMIT_CHASE_INITIAL_BPS`, `LIMIT_CHASE_STEP_BPS`, `LIMIT_CHASE_MAX_BPS`, and `LIMIT_CHASE_WAIT_SECONDS`, then falls back to market unless `LIMIT_CHASE_FALLBACK_MARKET=0`. Exchange-native stops remain the primary fast protection; the local watchdog is the repair/enforcement layer when venue state and ledger state diverge.

For live demo order-path latency checks, use:

```bash
CONFIRM_DEMO_ORDERS=1 PROBE_SYMBOL=BTCUSDT PROBE_COUNT=2 scripts/probe_bybit_demo_order_latency.py
```

The probe submits tiny far-from-touch post-only demo limit orders and cancels them immediately, reporting place/cancel latency. It is for demo order-path timing only and should not be used as alpha evidence.
