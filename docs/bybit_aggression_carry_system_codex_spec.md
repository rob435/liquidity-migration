# Current Bybit Research Spec

This file is intentionally short. It replaces the old composite
aggression/carry/momentum/live-runtime blueprint.

Read this before changing strategy, data ingestion, feature engineering, or
backtesting logic.

## Current Source Of Truth

The active alpha lead is documented in:

```text
docs/daily_close_fade.md
configs/volume_alpha.default.yaml
```

The active contract is:

```text
Strategy: daily-close short fade
Signal: 22:00 UTC
Ranking: day-to-date vol-adjusted return using only data available at 22:00
Entry: 60 equal 1m slices over [22:00, 23:00)
Entry price: average filled slice price
Stop: 20% above average entry, active from first fill + 15m
Adaptive exits: active from final add + 15m
Exit: flatten whole symbol
Re-entry: none in the same symbol/date
Universe: Bybit USDT linear perps, prior-liquidity ranks 31-150
Mode: research/backtest first, not real-money live trading
```

Current-top-universe backtests are benchmarks only. Promotion requires
point-in-time archive validation.

## Deprecated Scope

Do not rebuild the old system in this repo:

```text
SignalEngine
execution.py live order loop
state.py runtime state machine
alerting.py live Telegram bot behavior
runtime_monitor.py / runtime_validation.py
old aggression/carry/momentum/OI composite stack
real-money exchange submission
```

Bybit demo plumbing may exist for execution testing, but demo fills are not
alpha proof.

## Bybit Data Notes We Still Need

Use Bybit V5 USDT linear perps:

```text
category=linear
settleCoin=USDT
status=Trading
exclude prelisting contracts
paginate instruments-info
```

Useful public endpoints:

```text
GET /v5/market/instruments-info
GET /v5/market/kline
GET /v5/market/tickers
GET /v5/market/funding/history
GET /v5/market/open-interest
GET /v5/market/recent-trade
WebSocket publicTrade.{symbol}
Bybit public trade archives
```

Important caveat: Bybit V5 klines provide OHLCV and turnover, but not
taker-buy/taker-sell volume. Any future taker-aggression signal must use signed
public trades, not candles alone.

For the current daily-close fade, 1m klines are sufficient to test the
top-gainer ranking, TWAP entry model, and exit lifecycle. The proof-grade path
should derive those 1m bars from Bybit public trade archives so delisted
symbols and historical symbol/date membership are preserved.

## Data Layout

Research data stays under a configurable data root:

```text
klines_1m/date=YYYY-MM-DD/symbol=SYMBOL/part.parquet
klines_1h/date=YYYY-MM-DD/symbol=SYMBOL/part.parquet
instruments/date=YYYY-MM-DD/part.parquet
archive_trade_manifest/date=YYYY-MM-DD/part.parquet
reports/
```

Use Polars and Parquet for research data. Keep large generated datasets out of
git.

## Promotion Gate

Do not call the alpha confirmed until:

1. Point-in-time Bybit archive membership is complete.
2. Archive-derived 1m bars cover the eligible universe for each day.
3. The unchanged 22:00-23:00 TWAP contract survives train, validation, and OOS
   windows.
4. Forward paper/demo implements real slice-level TWAP accounting.
5. Forward audit compares expected slices, demo orders, fills, slippage, missed
   trades, sleeve attribution, and daily PnL.

## Secondary Research

The daily volume-rank alpha remains secondary background:

```text
docs/volume_alpha.md
```

Do not blend it with the daily-close fade until each standalone alpha clears
costs and point-in-time validation.
