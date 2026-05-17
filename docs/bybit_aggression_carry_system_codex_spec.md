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
threshold: top 40% dollar-volume rank migration
PIT liquidity rank: 31-150
excluded: stable/peg perps only, including failed peg remnants such as USTCUSDT
rank improvement: >= 150 versus prior 7d liquidity rank
turnover expansion: current turnover / prior 7d mean >= 6.0
event rank fraction cap: <= 0.90
event rank middle-band skip: disabled
coin day-return gate: daily_return_1d >= 0%
idiosyncratic move gate: daily_return_1d - market_median_return_1d >= +8%
regime gate: market_pct_up_1d <= 0.65 OR coin daily_return_1d clears the adaptive 16% +/- 1.5% hot-market band
strong-close gate: signal-day close location >= 0.45
PIT/listing age gate: >= 90 days
crowding veto: union_pathology same-entry-hour pathology filter
entry delay: 1 hour after daily signal close
max hold: 3 days
stop: 12% fixed
take profit: 25% fixed
gross exposure: 0.97
max active symbols: 5
symbol cooldown: 5 days
stop-pressure throttle: pause after 7 realized stops inside 10 days
realized-loss throttle: pause after 6 realized losses inside 5 days
cost multiplier: 3x base round-trip cost
```

The strategy is short-only because the best full-PIT evidence is in reversal after liquidity migration. Long continuation is not promoted unless a fresh full-PIT run clears costs, splits, drawdown, and report gates.

Current research frontier:

```text
data/research_reports/frontier_union_crowding_promoted_20260517
```

This is the adaptive hot-band liquidity-migration short with the `union_pathology`
same-hour crowding veto. It replaces the rejected March-specific crowding patch
as the active research and demo-default strategy, with +1950.72% total return, -10.74% max drawdown,
-4.64% worst 90d, +113.73% worst split, average split Sharpe-like 3.72, and
+177.58% OOS over the full-PIT no-funding research root.

Risk note: this was promoted into demo defaults on 2026-05-17 by explicit user
decision without waiting for a paper shadow cycle. That is acceptable only
because the client is demo-only. It is not real-money evidence.

## Reference Evidence

Legacy full-PIT baseline before the union crowding promotion:

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

The runner loops every `INTERVAL_SECONDS=60` by default. Each cycle:

1. Pulls the current Bybit USDT perpetual universe through rank 220 so the selected rank 31-150 strategy can be evaluated forward.
2. Excludes only stable/peg perps, including failed peg remnants such as USTCUSDT, before ranks/features are built.
3. Rebuilds recent 1h volume features from a 45-day lookback.
4. Exits existing demo positions first on fixed-stop/take-profit reconciliation, event decay, rank exit, or 3-day max hold.
5. Enters accepted liquidity-migration events after the 1-hour signal delay, subject to max-active, cooldown, stop-pressure, realized-loss-pressure, positive-day-return, residual-return, hot-band regime, close-location, listing-age, union crowding, and stale-entry gates. Stale entries are skipped after 15 minutes by default so demo fills stay close to the backtest entry timestamp.
6. Sizes each accepted coin from the same weight used by the backtest: `gross_exposure / max_active_symbols`, currently `0.97 / 5 = 19.40%` of current Bybit demo USDT equity. `--max-order-notional-pct-equity` is only an explicit override. The continuous runner defaults entry leverage to 2x for margin headroom without changing notional sizing.
7. Sends Telegram status with wallet equity, Bybit demo open positions, position value, and unrealized PnL when Telegram is enabled.
8. Writes expected/submitted order state into `event_demo_orders`, trade state into `event_demo_trades`, cycle telemetry into `event_demo_cycles`, and Markdown/JSON reports under `reports/event-demo`.

Order submission is still fail-closed: `--submit-orders` requires `--confirm-demo-orders`, `BYBIT_DEMO_API_KEY`, and `BYBIT_DEMO_API_SECRET`. Without those, the command is a dry-run scan.

Telegram may notify, but it must not approve or submit orders. The continuous runner now fails startup when `TELEGRAM_ENABLED=1` but Telegram or Bybit demo credentials are missing, because position/PnL reporting would be incomplete.
