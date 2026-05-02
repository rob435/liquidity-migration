# Bybit Aggression-Carry Crypto Perp Trading System — Codex Implementation Spec

Status note, 2026-05-02: this is retained as a Bybit data/source reference and
as background for future research. It is not the active implementation surface
after the strip-down. The active plan is `docs/volume_alpha.md`, and the current
repo intentionally tests one isolated volume alpha before any composite rebuild.

Version: 2026-05-01
Target venue: Bybit V5 API
Target product: USDT linear perpetuals (`category="linear"`)
Primary strategy type: cross-sectional long/short, mid-frequency, retail-sized
Primary alpha: taker aggression / signed trade flow
Secondary alphas: relative volume, funding carry, short-term momentum, liquidity/quality

This document is written to be fed to Codex as the implementation blueprint. Build the system incrementally. Do not skip the research, backtest, paper-trading, or risk-control gates.

This is not financial advice. The system is a design plan for research and engineering. Live use requires jurisdictional, tax, exchange, and operational review.

---

## 0. Critical Bybit-specific adjustment

The original alpha discussed was:

```text
EMA(market buys) / EMA(market sells)
```

On Binance, this can often be derived from candle fields that include taker-buy volume. On Bybit, the standard V5 kline endpoint gives OHLCV and turnover, but does **not** give taker-buy or taker-sell volume. Therefore:

```text
Do not build the aggression signal from Bybit klines alone.
Build a signed trade-flow aggregator using Bybit public trades.
```

Use:

```text
WebSocket: publicTrade.{symbol}
REST bootstrap: GET /v5/market/recent-trade
Historical backfill: Bybit archived historical trades if available from the Bybit data download site
```

The WebSocket public trade message includes `S`, the side of taker, with values `Buy` or `Sell`. Use that to build buy/sell quote-volume bars.

---

## 1. System objective

Build a Bybit-only, USDT-linear-perp trading system that ranks liquid perps by expected 4h–72h relative performance.

The live system should output target positions every 4 hours, based on 1-hour closed-bar features.

Core goal:

```text
Trade the strongest flow + volume + carry names long.
Trade the weakest flow + volume + carry names short.
Stay near market-neutral.
Control fees, funding, spread, slippage, and short-squeeze risk.
```

Initial production settings:

```text
Universe: 40–80 most liquid Bybit USDT linear perpetuals
Signal bars: 1h
Trade schedule: every 4h, after closed bar and data validation
Expected holding horizon: 12h–72h
Gross exposure: 0.50x in dust live; 1.00x normal initial; never above 1.50x until proven
Net exposure: target 0.00x, hard cap +/-0.10x
Position mode: one-way mode, positionIdx=0
Execution: maker-biased post-only limit orders; taker only for urgent risk reduction
```

---

## 2. Bybit API sources to use

Use official Bybit V5 API endpoints. Prefer `pybit` for initial implementation, but code should be wrapped behind exchange adapter interfaces so the rest of the strategy is not coupled to pybit.

### 2.1 Public market REST endpoints

Use these for research, bootstrap, and periodic refresh.

```text
GET /v5/market/instruments-info
Purpose:
    Get tradable symbols, launchTime, status, contractType, settleCoin,
    price tick, quantity step, min notional, max order qty, fundingInterval,
    funding caps, prelisting status.

Params:
    category=linear
    limit=1000
    cursor=<paginate until empty>

Important:
    There are more than 500 linear symbols; always paginate.
    Exclude status != Trading.
    Exclude isPreListing == true.
    Exclude non-USDT settle coins unless explicitly enabled.
```

```text
GET /v5/market/kline
Purpose:
    Historical OHLCV/turnover bars.

Params:
    category=linear
    symbol=<SYMBOL>
    interval=60 for hourly bars; optionally 1 or 5 for high-resolution research
    start=<ms>
    end=<ms>
    limit=1000

Fields:
    startTime, open, high, low, close, volume, turnover

Bybit linear contracts:
    volume = base coin units
    turnover = quote turnover, use this for comparable volume/liquidity
```

```text
GET /v5/market/recent-trade
Purpose:
    Bootstrap latest signed trades if WebSocket collector restarts.

Params:
    category=linear
    symbol=<SYMBOL>
    limit=1000

Fields:
    price, size, side, time, isBlockTrade, isRPITrade, seq

Use:
    side == Buy  => taker buyer; add price * size to buy_quote
    side == Sell => taker seller; add price * size to sell_quote

Caveat:
    This is recent-only. It is not a complete historical backfill.
```

```text
GET /v5/market/funding/history
Purpose:
    Historical funding rates.

Params:
    category=linear
    symbol=<SYMBOL>
    startTime=<ms>
    endTime=<ms>
    limit=200

Fields:
    fundingRate, fundingRateTimestamp

Important:
    Funding interval differs by symbol; get it from instruments-info.
```

```text
GET /v5/market/tickers
Purpose:
    Live/current price snapshot, bid/ask, 24h turnover/volume, open interest,
    current funding rate, next funding time.

Params:
    category=linear

Useful fields:
    lastPrice, markPrice, indexPrice, openInterest, openInterestValue,
    turnover24h, volume24h, fundingRate, nextFundingTime,
    bid1Price, ask1Price, bid1Size, ask1Size, fundingIntervalHour
```

```text
GET /v5/market/orderbook
Purpose:
    Spread, top-of-book liquidity, execution sanity checks.

Params:
    category=linear
    symbol=<SYMBOL>
    limit=1 or 25 for live spread; 50+ for deeper impact estimate

Useful fields:
    b[0] best bid price/size
    a[0] best ask price/size
    ts, u, seq, cts
```

```text
GET /v5/market/open-interest
Purpose:
    Historical open interest features and squeeze-risk checks.

Params:
    category=linear
    symbol=<SYMBOL>
    intervalTime=1h or 4h
    startTime=<ms>
    endTime=<ms>
    limit=200
```

### 2.2 Private/account REST endpoints

Use these for live operation.

```text
GET /v5/account/wallet-balance
Purpose:
    Account equity, available balance, margin balance, total initial margin,
    maintenance margin, unrealized perp PnL.

Params:
    accountType=UNIFIED
```

```text
GET /v5/account/fee-rate
Purpose:
    Actual maker/taker fee rates for each symbol/account.

Params:
    category=linear
    symbol=<SYMBOL>
```

```text
POST /v5/order/create
Purpose:
    Submit live orders.

Required for perps:
    category=linear
    symbol=<SYMBOL>
    side=Buy or Sell
    orderType=Limit or Market
    qty=<positive string quantity>
    price=<required for limit>
    timeInForce=PostOnly for passive orders, IOC for urgent market-like execution
    positionIdx=0 for one-way mode
    reduceOnly=true for any order intended to reduce/close position
    orderLinkId=<unique id max 36 chars>

Important:
    Place-order acknowledgment is asynchronous. Confirm true order/fill state from private WebSocket order/execution streams.
```

### 2.3 WebSockets

Use WebSockets for all live data and private state updates. REST is for bootstrap, periodic refresh, and fallback.

Public streams:

```text
publicTrade.{symbol}
    Main input for taker aggression.
    Collect every trade and aggregate into 1m signed-flow bars.

kline.1.{symbol} or kline.60.{symbol}
    Optional live OHLCV stream.
    Use confirm=true only for closed candles.

orderbook.1.{symbol} or orderbook.25.{symbol}
    Live spread and top liquidity.

tickers.{symbol}
    Live funding, mark price, open interest, 24h turnover, bid/ask.
```

Private streams:

```text
order.linear
    Order lifecycle state.

execution.linear
    Real fills, fee, maker/taker flag, execution price/qty.

position.linear
    Current positions and liquidation/margin fields.

wallet
    Equity and wallet state.
```

---

## 3. Repository architecture

Create a clean modular project.

```text
bybit_aggression_system/
    README.md
    pyproject.toml
    config/
        default.yaml
        paper.yaml
        live.yaml
    src/
        main.py
        settings.py
        logging_config.py
        exchange/
            bybit_rest.py
            bybit_ws.py
            models.py
            rate_limiter.py
            rounding.py
        data/
            store.py
            schemas.py
            kline_loader.py
            funding_loader.py
            trade_collector.py
            trade_aggregator.py
            open_interest_loader.py
            quality_checks.py
        research/
            feature_engine.py
            signal_engine.py
            ic_analysis.py
            backtest_vectorized.py
            backtest_event_driven.py
            reports.py
        strategy/
            universe.py
            features.py
            signals.py
            portfolio.py
            risk.py
            costs.py
            scheduler.py
        execution/
            order_manager.py
            execution_algo.py
            reconciliation.py
            kill_switch.py
        live/
            live_loop.py
            monitor.py
            paper_broker.py
        tests/
            test_trade_aggregation.py
            test_features.py
            test_universe.py
            test_portfolio.py
            test_risk.py
            test_rounding.py
            test_no_leakage.py
```

Preferred stack:

```text
Python 3.11+
pandas or polars
numpy
scipy
pydantic
pybit
websockets or pybit websocket client
sqlite for local prototype, Postgres for serious use
pytest
```

Do not hard-code API keys. Read them from environment variables.

```text
BYBIT_API_KEY
BYBIT_API_SECRET
BYBIT_TESTNET=true/false
```

---

## 4. Data storage schemas

Codex should create schemas with correct primary keys and indexes.

### 4.1 instruments

```sql
CREATE TABLE instruments (
    symbol TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    contract_type TEXT,
    status TEXT,
    base_coin TEXT,
    quote_coin TEXT,
    settle_coin TEXT,
    launch_time_ms INTEGER,
    delivery_time_ms INTEGER,
    tick_size REAL,
    qty_step REAL,
    min_order_qty REAL,
    min_notional_value REAL,
    max_order_qty REAL,
    max_market_order_qty REAL,
    funding_interval_min INTEGER,
    upper_funding_rate REAL,
    lower_funding_rate REAL,
    is_prelisting BOOLEAN,
    updated_at_ms INTEGER
);
```

### 4.2 klines_1h

```sql
CREATE TABLE klines_1h (
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume_base REAL NOT NULL,
    turnover_quote REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (ts_ms, symbol)
);
```

### 4.3 raw_public_trades

Use this only if storage budget allows. Otherwise store aggregated flow bars and keep raw trades in rolling local files.

```sql
CREATE TABLE raw_public_trades (
    trade_id TEXT NOT NULL,
    seq TEXT,
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size_base REAL NOT NULL,
    quote_value REAL NOT NULL,
    is_block_trade BOOLEAN,
    is_rpi_trade BOOLEAN,
    PRIMARY KEY (symbol, trade_id)
);
```

### 4.4 signed_flow_1m

```sql
CREATE TABLE signed_flow_1m (
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    buy_quote REAL NOT NULL,
    sell_quote REAL NOT NULL,
    buy_base REAL NOT NULL,
    sell_base REAL NOT NULL,
    trade_count_buy INTEGER NOT NULL,
    trade_count_sell INTEGER NOT NULL,
    vwap_buy REAL,
    vwap_sell REAL,
    PRIMARY KEY (ts_ms, symbol)
);
```

### 4.5 signed_flow_1h

```sql
CREATE TABLE signed_flow_1h (
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    buy_quote REAL NOT NULL,
    sell_quote REAL NOT NULL,
    total_quote REAL NOT NULL,
    signed_quote REAL NOT NULL,
    imbalance REAL NOT NULL,
    trade_count INTEGER NOT NULL,
    PRIMARY KEY (ts_ms, symbol)
);
```

Where:

```text
total_quote = buy_quote + sell_quote
signed_quote = buy_quote - sell_quote
imbalance = signed_quote / max(total_quote, eps)
```

### 4.6 funding

```sql
CREATE TABLE funding (
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    funding_rate REAL NOT NULL,
    funding_interval_min INTEGER,
    funding_rate_8h_equiv REAL,
    PRIMARY KEY (ts_ms, symbol)
);
```

### 4.7 ticker_snapshots

```sql
CREATE TABLE ticker_snapshots (
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    last_price REAL,
    mark_price REAL,
    index_price REAL,
    bid1_price REAL,
    ask1_price REAL,
    bid1_size REAL,
    ask1_size REAL,
    open_interest REAL,
    open_interest_value REAL,
    turnover_24h REAL,
    volume_24h REAL,
    funding_rate REAL,
    next_funding_time_ms INTEGER,
    PRIMARY KEY (ts_ms, symbol)
);
```

### 4.8 features

```sql
CREATE TABLE features_1h (
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    aggression_raw REAL,
    aggression_z REAL,
    rel_volume_raw REAL,
    rel_volume_z REAL,
    momentum_raw REAL,
    momentum_z REAL,
    carry_raw REAL,
    carry_z REAL,
    quality_raw REAL,
    quality_z REAL,
    oi_impulse_raw REAL,
    oi_impulse_z REAL,
    composite_score REAL,
    PRIMARY KEY (ts_ms, symbol)
);
```

### 4.9 orders/fills/positions/pnl

```sql
CREATE TABLE target_positions (
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    target_weight REAL NOT NULL,
    target_notional REAL NOT NULL,
    score REAL,
    reason TEXT,
    PRIMARY KEY (ts_ms, symbol)
);

CREATE TABLE orders (
    order_link_id TEXT PRIMARY KEY,
    order_id TEXT,
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    price REAL,
    qty REAL NOT NULL,
    time_in_force TEXT,
    reduce_only BOOLEAN,
    status TEXT,
    error TEXT
);

CREATE TABLE fills (
    exec_id TEXT PRIMARY KEY,
    order_link_id TEXT,
    order_id TEXT,
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    qty REAL NOT NULL,
    value REAL NOT NULL,
    fee REAL,
    fee_rate REAL,
    is_maker BOOLEAN
);

CREATE TABLE positions (
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    notional REAL NOT NULL,
    entry_price REAL,
    mark_price REAL,
    unrealized_pnl REAL,
    PRIMARY KEY (ts_ms, symbol)
);

CREATE TABLE pnl_snapshots (
    ts_ms INTEGER PRIMARY KEY,
    total_equity REAL,
    available_balance REAL,
    gross_exposure REAL,
    net_exposure REAL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    fee_pnl REAL,
    funding_pnl REAL,
    slippage_pnl REAL
);
```

---

## 5. Universe selection

Universe is refreshed every 24h and validated every trading cycle.

### 5.1 Hard symbol filters

Include symbol only if all true:

```text
category == linear
contractType == LinearPerpetual
status == Trading
settleCoin == USDT
isPreListing == false
launchTime older than MIN_AGE_DAYS
fundingInterval is not null
minNotionalValue is available
qtyStep and tickSize are available
```

Initial config:

```yaml
universe:
  min_age_days: 90
  max_symbols: 80
  min_turnover_24h_usdt: 20000000
  min_open_interest_value_usdt: 5000000
  max_spread_bps: 8
  exclude_symbols:
    - USDCUSDT
    - USDEUSDT
    - FDUSDUSDT
  exclude_prelisting: true
  settle_coin: USDT
```

### 5.2 Liquidity ranking

Rank eligible symbols by:

```text
liquidity_score = 0.70 * zscore(log(turnover_24h)) + 0.30 * zscore(log(open_interest_value))
```

Keep top `max_symbols`.

### 5.3 Spread filter

From ticker or orderbook:

```text
spread_bps = 10000 * (ask1_price - bid1_price) / mid
mid = (ask1_price + bid1_price) / 2
```

Exclude if:

```text
spread_bps > max_spread_bps
bid1_size or ask1_size missing
mid <= 0
```

---

## 6. Feature engineering

All features are computed on closed 1-hour bars. Signals for bar `t` may only trade after bar `t` is confirmed closed and stored.

### 6.1 Taker aggression signal

Input: `signed_flow_1h` from Bybit public trades.

For each symbol and hour:

```text
buy_quote[t]  = sum(price * size for trades where side == Buy)
sell_quote[t] = sum(price * size for trades where side == Sell)
```

By default, exclude block trades and RPI trades from the aggression calculation:

```text
if isBlockTrade == true: ignore for aggression
if isRPITrade == true: ignore for aggression
```

Reason: block/RPI prints can have different microstructure meaning. Store them separately later if useful.

Compute:

```python
eps = max(1.0, 0.001 * rolling_median(total_quote, 168))

buy_ema_6h  = ema(buy_quote, span=6)
sell_ema_6h = ema(sell_quote, span=6)

buy_ema_24h  = ema(buy_quote, span=24)
sell_ema_24h = ema(sell_quote, span=24)

aggr_fast = log((buy_ema_6h + eps) / (sell_ema_6h + eps))
aggr_slow = log((buy_ema_24h + eps) / (sell_ema_24h + eps))

aggression_raw = 0.70 * aggr_fast + 0.30 * aggr_slow
```

Normalize cross-sectionally each timestamp:

```python
aggression_z = cross_sectional_robust_zscore(aggression_raw)
```

Robust z-score function:

```python
def robust_zscore(x):
    med = median(x)
    mad = median(abs(x - med))
    if mad <= 1e-12:
        return zeros_like(x)
    return clip(0.6745 * (x - med) / mad, -3, 3)
```

Interpretation:

```text
aggression_z > 0 = more aggressive buying than peers
aggression_z < 0 = more aggressive selling than peers
```

### 6.2 Relative volume signal

Use Bybit kline `turnover`, not base `volume`, because turnover is quote-denominated and comparable.

```python
qvol = turnover_quote
qvol_fast = ema(qvol, span=6)
qvol_slow = ema(qvol, span=72)
rel_volume_raw = log((qvol_fast + eps) / (qvol_slow + eps))
rel_volume_z = cross_sectional_robust_zscore(rel_volume_raw)
```

Use relative volume as a confirmation layer for aggression:

```python
volume_multiplier = 1.0 + 0.25 * clip(rel_volume_z, -1.0, 2.0)
aggression_confirmed = aggression_z * volume_multiplier
aggression_confirmed = clip(aggression_confirmed, -3, 3)
```

### 6.3 Momentum signal

Use close prices from 1h klines.

```python
ret_12h = log(close[t] / close[t-12])
ret_24h = log(close[t] / close[t-24])
ret_72h = log(close[t] / close[t-72])

momentum_raw = 0.50 * ret_12h + 0.30 * ret_24h + 0.20 * ret_72h
momentum_z = cross_sectional_robust_zscore(momentum_raw)
```

Do not use very long momentum in the first version. The goal is short-term continuation, not multi-month trend following.

### 6.4 Funding carry signal

Funding direction:

```text
positive funding: longs pay shorts
negative funding: shorts pay longs
```

Therefore:

```text
Long preference = negative funding
Short preference = positive funding
```

Bybit symbols may have different funding intervals. Normalize to 8-hour equivalent:

```python
funding_rate_8h_equiv = funding_rate * (480 / funding_interval_min)
carry_raw = -ema(funding_rate_8h_equiv, span=3)
carry_z = cross_sectional_robust_zscore(carry_raw)
```

Carry guard:

```python
# Do not let carry fight very strong aggression too much.
if sign(carry_z) != sign(aggression_z) and abs(aggression_z) > 1.5:
    carry_z_adjusted = 0.50 * carry_z
else:
    carry_z_adjusted = carry_z
```

### 6.5 Liquidity/quality signal

Use current/rolling Bybit ticker data:

```python
liq_raw = log(rolling_median(turnover_24h, 24) + 1)
oi_raw  = log(rolling_median(open_interest_value, 24) + 1)
quality_raw = 0.70 * liq_raw + 0.30 * oi_raw
quality_z = cross_sectional_robust_zscore(quality_raw)
```

This is not a pure alpha. It is a risk tilt. It reduces exposure to tiny, unstable, expensive names.

### 6.6 Open-interest impulse signal, optional but recommended

This is a useful Bybit-specific confirmation feature because tickers and open-interest history are accessible.

```python
oi_value = open_interest_value
price_ret_12h = log(close[t] / close[t-12])
oi_ret_12h = log((oi_value[t] + 1) / (oi_value[t-12] + 1))

# Positive when price and OI rise together, or price falls while OI rises on the short side.
oi_impulse_raw = sign(price_ret_12h) * oi_ret_12h

# But use cross-sectional rank only; do not over-weight it.
oi_impulse_z = cross_sectional_robust_zscore(oi_impulse_raw)
```

Interpretation:

```text
Positive OI impulse = move has fresh positioning behind it.
Negative OI impulse = move may be closing/covering, less durable.
```

Do not let this dominate the system. It is a small modifier.

---

## 7. Composite score

First production candidate:

```python
score_raw = (
    0.42 * aggression_confirmed +
    0.18 * momentum_z +
    0.20 * carry_z_adjusted +
    0.12 * quality_z +
    0.08 * oi_impulse_z
)

score = cross_sectional_demean(clip(score_raw, -3, 3))
```

Rationale:

```text
Aggression is the main alpha.
Momentum captures continuation/reflexivity.
Carry captures funding/crowding.
Quality keeps the book in tradable names.
OI impulse confirms whether positioning is entering.
```

Do not fit these weights with ML in version 1. After enough history, test equal-weight, the above hand weights, and basic ridge regression only. If the signal does not work with simple weights, do not hide the problem with a model.

---

## 8. Portfolio construction

### 8.1 Candidate buckets

At each rebalance timestamp:

```python
long_bucket = top 20% of symbols by score
short_bucket = bottom 20% of symbols by score
```

Require:

```text
minimum absolute score for entry: 0.25
minimum symbols per side: 6
maximum symbols per side: 16
```

If not enough symbols pass quality checks, reduce gross rather than forcing bad trades.

### 8.2 Volatility-adjusted raw weights

Compute realized volatility on 1h returns:

```python
vol_7d = std(returns_1h over last 168 hours) * sqrt(24)
vol_floor = median(vol_7d) * 0.50
vol_adj = max(vol_7d, vol_floor)
```

Raw side weights:

```python
raw_weight_i = abs(score_i) / vol_adj_i
```

Normalize:

```python
sum(long_weights) = +gross / 2
sum(short_weights) = -gross / 2
```

Initial gross schedule:

```text
paper: simulated only
live_dust: 0.25x gross
live_validation: 0.50x gross
normal: 1.00x gross
max_after_evidence: 1.50x gross
```

### 8.3 Position caps

```yaml
risk:
  max_gross_exposure: 1.0
  max_gross_exposure_validated: 1.5
  max_net_exposure_abs: 0.10
  max_single_position_weight: 0.05
  max_single_low_liquidity_weight: 0.015
  max_symbol_position_vs_turnover_24h: 0.0002   # 0.02% of 24h turnover
  max_symbol_position_vs_oi_value: 0.001        # 0.10% of OI value
```

### 8.4 Beta neutrality

Estimate rolling betas to BTCUSDT and ETHUSDT using 1h returns over 7 days.

```python
beta_btc_i = cov(ret_i, ret_btc) / var(ret_btc)
beta_eth_i = cov(ret_i, ret_eth) / var(ret_eth)

portfolio_beta_btc = sum(weight_i * beta_btc_i)
portfolio_beta_eth = sum(weight_i * beta_eth_i)
```

Target:

```text
abs(portfolio_beta_btc) <= 0.10
abs(portfolio_beta_eth) <= 0.10
```

Simple correction:

```text
If beta too positive, reduce long weights or add small BTC/ETH short hedge.
If beta too negative, reduce short weights or add small BTC/ETH long hedge.
```

Do not over-optimize hedging in version 1. Excessive hedging causes turnover and hides true strategy behavior.

---

## 9. Risk engine

Risk engine must run before every order batch and continuously during live operation.

### 9.1 Account-level kill rules

```yaml
kill_rules:
  max_daily_loss_pct: 0.03
  max_weekly_loss_pct: 0.06
  max_monthly_loss_pct: 0.10
  max_total_drawdown_pct: 0.15
  max_margin_usage_pct: 0.35
  min_available_balance_pct: 0.25
```

Actions:

```text
Daily loss <= -3%:
    cancel all non-reduce-only orders
    stop new entries for 24h
    allow reduce-only exits

Weekly loss <= -6%:
    reduce target gross by 50%
    stop new entries for 48h unless manually reset

Monthly loss <= -10%:
    reduce target gross to 0.25x
    require research review

Total drawdown <= -15%:
    flatten positions using reduce-only orders
    disable live trading
```

### 9.2 Market stress scaler

Compute:

```python
btc_ret_24h = log(BTCUSDT_close[t] / BTCUSDT_close[t-24])
btc_vol_24h = std(BTCUSDT_1h_returns[t-24:t])
btc_vol_median_30d = median(rolling_24h_btc_vol over 30d)
median_spread_bps = median(universe spread_bps)
spread_stress = median_spread_bps / rolling_median(median_spread_bps, 30d)
breadth_negative = percent of universe with 24h return < 0
funding_stress = median(abs(funding_rate_8h_equiv))
```

Gross scaler:

```python
scale = 1.0

if btc_ret_24h < -0.05:
    scale = min(scale, 0.50)
if btc_vol_24h > 2.0 * btc_vol_median_30d:
    scale = min(scale, 0.60)
if spread_stress > 2.0:
    scale = min(scale, 0.50)
if breadth_negative > 0.85:
    scale = min(scale, 0.60)
if funding_stress > 0.0015:  # tune after research
    scale = min(scale, 0.75)
```

### 9.3 Short-squeeze guard

Block new shorts if all are true:

```text
symbol return 24h > +25%
aggression_z > +1.5
rel_volume_z > +1.5
funding_rate_8h_equiv is very positive or rising
spread_bps > median spread for symbol
```

If already short and conditions trigger:

```text
Reduce position by 50% immediately using reduce-only order.
Fully exit if price moves another +10% or score flips positive.
```

### 9.4 Funding guard

For each candidate:

```python
expected_holding_hours = 24
expected_funding_pnl = -position_side * funding_rate_8h_equiv * (expected_holding_hours / 8)
```

Sign convention:

```text
Long pays when funding positive.
Short pays when funding negative.
```

Block/halve trade if:

```text
expected funding cost > 50% of estimated alpha edge
```

In version 1, if estimated alpha edge is not calibrated, use a simple guard:

```text
For longs: block if funding_rate_8h_equiv > +0.0015 and aggression_z < +1.0
For shorts: block if funding_rate_8h_equiv < -0.0015 and aggression_z > -1.0
```

### 9.5 Data quality kill switch

Stop new entries if:

```text
publicTrade stream gap > 5 minutes for any symbol currently traded
kline missing for current closed hour
funding/ticker data older than 10 minutes
private execution stream disconnected
position reconciliation mismatch > 1% notional
order state unknown for > 60 seconds
Bybit REST returns repeated rate-limit or auth errors
```

Allow reduce-only exits even when data quality is degraded, but use conservative sizing and slippage checks.

---

## 10. Execution design

Execution must be cost-aware. This system should not spray market orders.

### 10.1 Order sizing and rounding

Before sending any order:

```python
qty = abs(target_position_qty - current_position_qty)
qty = floor_to_step(qty, qty_step)
price = round_to_tick(price, tick_size)
notional = qty * price
```

Reject if:

```text
qty < minOrderQty
notional < minNotionalValue
qty > maxOrderQty unless split
symbol not in current valid instrument cache
```

### 10.2 No-trade band

Do not trade tiny target changes.

```yaml
execution:
  rebalance_interval_hours: 4
  min_trade_notional_usdt: 25
  no_trade_weight_band: 0.003       # 0.30% of equity
  no_trade_notional_band_pct: 0.0025
```

Trade only if:

```python
abs(target_notional - current_notional) > max(
    min_trade_notional_usdt,
    no_trade_weight_band * account_equity
)
```

### 10.3 Passive maker-biased execution

Default order style:

```text
Limit + timeInForce=PostOnly
```

For buys:

```python
limit_price = best_bid
```

For sells:

```python
limit_price = best_ask
```

Repricing loop:

```text
Place post-only order.
Wait 30–90 seconds.
If not filled and signal still valid:
    cancel/replace at updated best bid/ask.
After max_reprices, leave unfilled unless position is risk-reducing.
```

Initial parameters:

```yaml
execution:
  passive_wait_seconds: 45
  max_reprices_per_symbol: 4
  max_live_orders_per_symbol: 2
  post_only_default: true
  use_taker_for_entries: false
  use_taker_for_risk_reduction: true
```

### 10.4 Urgent exits

Use IOC/market-like execution only when:

```text
kill switch triggered
reduce-only exit required
private/account risk issue
short-squeeze guard triggered
score flips hard against current position
```

Use Bybit market order slippage tolerance if supported in implementation:

```text
slippageToleranceType=Percent
slippageTolerance=0.10 to 0.50 depending on liquidity
```

Do not use market orders for normal entries.

### 10.5 Order IDs

Use deterministic unique `orderLinkId`:

```text
AGC_<timestamp_ms>_<symbol>_<side>_<shortuuid>
```

Must be <= 36 chars.

Track all orders until terminal state.

### 10.6 Async acknowledgment rule

Bybit order-create response means request accepted, not fully executed. Therefore:

```text
Never update positions from order ack.
Update positions only from private execution stream + reconciliation.
```

---

## 11. Cost model

Model costs pessimistically.

### 11.1 Fee model

Fetch actual fee rates:

```text
GET /v5/account/fee-rate?category=linear&symbol=<SYMBOL>
```

For backtest, use:

```text
maker_fee = actual makerFeeRate if available, else conservative default
thaker_fee = actual takerFeeRate if available, else conservative default
```

Typo note for Codex: variable name should be `taker_fee`, not `thaker_fee`.

### 11.2 Spread/slippage

Estimate live spread:

```python
spread_bps = 10000 * (ask1 - bid1) / mid
half_spread_cost = spread_bps / 2
```

For backtest:

```text
Passive fill cost:
    maker fee + adverse selection estimate

Taker fill cost:
    taker fee + half spread + slippage estimate
```

Initial conservative assumptions:

```yaml
cost_model:
  maker_fill_probability: 0.60
  maker_adverse_selection_bps: 1.0
  taker_slippage_bps_liquid: 2.0
  taker_slippage_bps_mid: 5.0
  taker_slippage_bps_tail: 10.0
  stress_cost_multiplier: 2.0
```

Backtest must run under:

```text
base costs
2x costs
3x costs
all-taker costs
low-maker-fill scenario
```

If the system only works with perfect post-only fills, reject it.

---

## 12. Backtesting plan

### 12.1 Research backtest

Purpose: determine whether signals have standalone predictive value.

For each timestamp and symbol, compute forward returns:

```text
forward_return_4h
forward_return_12h
forward_return_24h
forward_return_72h
```

Shift all signals correctly:

```text
Feature at closed hour t may predict return from t+1 onward.
Never trade on same-bar information before the bar closes.
```

Measure:

```text
Spearman IC per timestamp
mean IC
IC t-stat
IC hit rate
quantile return spread: top bucket minus bottom bucket
monthly consistency
PnL by symbol
PnL by side: long vs short
PnL by market regime
```

Acceptance gates for standalone signals:

```text
mean IC > 0 for aggression signal
positive top-minus-bottom return after base estimated costs
signal not dependent on one coin or one month
no severe parameter fragility
```

### 12.2 Portfolio backtest

Backtest the full portfolio:

```text
Universe refresh daily.
Rebalance every 4h.
Apply position caps.
Apply no-trade band.
Apply fee, spread, slippage, and funding.
Apply market stress scaler.
Track actual long/short/funding/fee/slippage components separately.
```

Acceptance gates:

```text
Profitable under 2x cost model.
Drawdown acceptable at 1.0x gross.
Long book and short book both contribute over time.
Fees consume less than 40% of gross alpha.
No single symbol contributes more than 20% of total PnL.
PnL survives excluding BTC/ETH/SOL.
PnL survives excluding the best month.
```

### 12.3 Event-driven backtest

Purpose: validate execution assumptions.

Simulate:

```text
post-only resting orders
maker/taker fill ratio
partial fills
cancel/replace latency
funding payments
spread-based rejects
position rounding
min notional rejects
rate-limit delays
```

Reject strategy if event-driven PnL differs materially from vectorized PnL without explainable reason.

---

## 13. Live rollout plan

### Stage 0: data collector only

```text
Duration: at least 2 weeks; preferably 30+ days.
Capital: none.
Goal: collect Bybit publicTrade data and create signed-flow bars.
```

Required checks:

```text
No trade-stream gaps.
1m and 1h aggregation matches expected total activity.
Kline turnover and signed-flow total_quote are directionally consistent.
Storage/restart recovery works.
```

### Stage 1: paper trading

```text
Duration: 30–60 days.
Capital: none.
Goal: verify signals, target positions, theoretical fills, cost assumptions.
```

Use real Bybit public and private-like paper broker state.

Do not use Bybit testnet market data for performance validation. Testnet is useful for order plumbing, not realistic liquidity/performance.

### Stage 2: dust live

```text
Duration: 2–4 weeks.
Gross: 0.25x.
Capital: smallest meaningful amount.
Goal: verify auth, orders, fills, fees, funding, reconciliation, and kill switches.
```

No performance conclusions yet.

### Stage 3: validation live

```text
Duration: 1–2 months.
Gross: 0.50x–1.00x.
Goal: compare live slippage, maker ratio, fees, fill quality, and signal decay to backtest.
```

### Stage 4: normal live

```text
Gross: 1.00x.
Max: 1.50x only after statistical evidence and operational stability.
```

---

## 14. Configuration file template

Codex should create `config/default.yaml` like this:

```yaml
exchange:
  name: bybit
  category: linear
  settle_coin: USDT
  testnet: true
  account_type: UNIFIED
  position_idx: 0
  recv_window_ms: 5000

universe:
  min_age_days: 90
  max_symbols: 80
  min_turnover_24h_usdt: 20000000
  min_open_interest_value_usdt: 5000000
  max_spread_bps: 8
  exclude_prelisting: true
  exclude_symbols:
    - USDCUSDT
    - USDEUSDT
    - FDUSDUSDT

features:
  bar_interval: 1h
  aggression_fast_span_h: 6
  aggression_slow_span_h: 24
  volume_fast_span_h: 6
  volume_slow_span_h: 72
  momentum_windows_h: [12, 24, 72]
  momentum_weights: [0.50, 0.30, 0.20]
  carry_ema_span: 3
  oi_impulse_window_h: 12
  robust_z_clip: 3.0
  exclude_block_trades_from_aggression: true
  exclude_rpi_trades_from_aggression: true

signals:
  weights:
    aggression_confirmed: 0.42
    momentum: 0.18
    carry: 0.20
    quality: 0.12
    oi_impulse: 0.08
  min_abs_score_entry: 0.25
  long_quantile: 0.20
  short_quantile: 0.20

portfolio:
  rebalance_interval_h: 4
  base_gross_exposure: 1.0
  max_gross_exposure: 1.0
  max_gross_exposure_validated: 1.5
  max_net_exposure_abs: 0.10
  max_single_position_weight: 0.05
  max_single_low_liquidity_weight: 0.015
  max_position_vs_turnover_24h: 0.0002
  max_position_vs_open_interest_value: 0.001
  volatility_window_h: 168
  beta_window_h: 168
  max_beta_btc_abs: 0.10
  max_beta_eth_abs: 0.10

risk:
  max_daily_loss_pct: 0.03
  max_weekly_loss_pct: 0.06
  max_monthly_loss_pct: 0.10
  max_total_drawdown_pct: 0.15
  max_margin_usage_pct: 0.35
  min_available_balance_pct: 0.25
  data_gap_public_trade_seconds: 300
  data_gap_private_ws_seconds: 60
  short_squeeze_return_24h: 0.25
  short_squeeze_aggression_z: 1.5
  short_squeeze_rel_volume_z: 1.5

execution:
  min_trade_notional_usdt: 25
  no_trade_weight_band: 0.003
  no_trade_notional_band_pct: 0.0025
  post_only_default: true
  passive_wait_seconds: 45
  max_reprices_per_symbol: 4
  max_live_orders_per_symbol: 2
  use_taker_for_entries: false
  use_taker_for_risk_reduction: true
  urgent_exit_slippage_percent: 0.25

cost_model:
  maker_adverse_selection_bps: 1.0
  taker_slippage_bps_liquid: 2.0
  taker_slippage_bps_mid: 5.0
  taker_slippage_bps_tail: 10.0
  stress_cost_multiplier: 2.0

storage:
  database_url: sqlite:///data/bybit_aggression.db
  store_raw_trades: false
  raw_trade_retention_days: 7

logging:
  level: INFO
  write_jsonl: true
```

---

## 15. Main live loop pseudocode

```python
while True:
    wait_until_next_rebalance_time()

    # 1. Validate data freshness
    assert_public_trade_stream_fresh()
    assert_private_streams_fresh()
    assert_latest_closed_1h_bars_available()

    # 2. Refresh state
    instruments = load_instrument_cache()
    tickers = fetch_or_load_latest_tickers()
    wallet = fetch_wallet_balance()
    positions = reconcile_positions()
    fees = refresh_fee_rates_if_needed()

    # 3. Build universe
    universe = build_universe(instruments, tickers, config)

    # 4. Compute features and scores
    features = compute_features(universe, latest_closed_ts)
    scores = compute_composite_scores(features)

    # 5. Build target portfolio
    targets = construct_portfolio(scores, positions, wallet, config)
    targets = beta_neutralize(targets)
    targets = apply_position_caps(targets, tickers, instruments)

    # 6. Risk overlays
    stress_scale = compute_market_stress_scale()
    targets = scale_gross(targets, stress_scale)
    targets = apply_kill_rules(targets, wallet, pnl_state)
    targets = apply_short_squeeze_guard(targets, features, positions)
    targets = apply_funding_guard(targets, features)

    # 7. Generate orders
    orders = diff_positions_to_orders(targets, positions)
    orders = apply_no_trade_band(orders, wallet)
    orders = round_orders_to_bybit_rules(orders, instruments)
    orders = reject_invalid_orders(orders)

    # 8. Execute
    cancel_stale_orders()
    submit_post_only_orders(orders)
    manage_repricing_loop()

    # 9. Reconcile and log
    reconcile_orders_fills_positions()
    write_pnl_snapshot()
    write_risk_report()
```

---

## 16. Implementation tasks for Codex

Build in this order.

### Task 1: Project scaffold

Create repo structure, config loader, logging, database connection, and pytest setup.

Acceptance:

```text
pytest runs.
config/default.yaml loads.
logging works.
database tables can be created idempotently.
```

### Task 2: Bybit REST adapter

Implement:

```text
get_instruments_info(category="linear") with pagination
get_klines(symbol, interval, start, end, limit)
get_recent_trades(symbol, limit)
get_funding_history(symbol, start, end)
get_tickers(category="linear")
get_orderbook(symbol, limit)
get_open_interest(symbol, intervalTime, start, end)
get_wallet_balance(accountType="UNIFIED")
get_fee_rate(category="linear", symbol)
place_order(...)
cancel_order(...)
cancel_all(...)
```

Acceptance:

```text
Handles retCode != 0.
Handles rate-limit headers where accessible.
Retries idempotent GETs with exponential backoff.
Does not retry non-idempotent POST without explicit orderLinkId safety.
Parses numeric fields to Decimal or float consistently.
```

### Task 3: Instrument cache and rounding

Implement:

```text
instrument pagination
symbol filters
round_price_to_tick
floor_qty_to_step
min notional checks
max order qty splitting
```

Acceptance:

```text
Unit tests for BTCUSDT-like and small-cap-like tick/step sizes.
No order can be generated below minNotionalValue.
No quantity violates qtyStep.
```

### Task 4: WebSocket trade collector

Implement:

```text
subscribe to publicTrade.{symbol} for all universe symbols
parse side, price, size, time, trade id, block/RPI flags
write to signed_flow_1m aggregator
handle reconnects
bootstrap recent trades after reconnect
track last message timestamp per symbol
```

Acceptance:

```text
Creates signed_flow_1m bars.
No double-counting after reconnect.
Can recover from WebSocket disconnect.
Gap detector triggers if no trades for active symbols beyond threshold.
```

### Task 5: Historical loaders

Implement:

```text
kline loader with pagination
funding history loader with pagination
open interest loader with pagination
optional historical trade archive importer
```

Acceptance:

```text
Data is sorted ascending internally.
No duplicate primary keys.
Closed bars only.
Funding interval normalization works.
```

### Task 6: Feature engine

Implement all features in Section 6.

Acceptance:

```text
No same-bar leakage.
Features use only data <= feature timestamp.
Robust z-score stable with missing values.
Features handle symbols with insufficient history by returning NaN and excluding them.
```

### Task 7: Signal and portfolio engine

Implement:

```text
composite score
long/short buckets
vol-adjusted weights
gross/net caps
position caps
BTC/ETH beta estimates
market stress scaling
```

Acceptance:

```text
Sum absolute weights <= gross limit.
Net exposure within cap.
No symbol exceeds position cap.
No illiquid symbol gets oversized.
Beta cap correction works or safely scales down.
```

### Task 8: Risk engine

Implement kill rules, short-squeeze guard, funding guard, data freshness guard, margin guard.

Acceptance:

```text
Unit tests simulate each kill condition.
Risk engine can force target gross to zero.
Reduce-only exits still allowed when entries blocked.
```

### Task 9: Backtesting

Implement research and portfolio backtests.

Acceptance:

```text
Computes IC and quantile spreads.
Runs full portfolio backtest with funding and fees.
Runs cost sensitivity: base, 2x, 3x, all-taker.
Produces report with long PnL, short PnL, funding PnL, fee PnL, slippage PnL.
```

### Task 10: Paper broker

Implement paper execution using real Bybit top-of-book and configurable maker/taker fill assumptions.

Acceptance:

```text
Tracks paper orders, fills, positions, PnL.
Can run from live market data without placing real orders.
Produces same tables as live for comparison.
```

### Task 11: Live execution

Implement order manager and passive execution algo.

Acceptance:

```text
Uses orderLinkId.
Uses PostOnly for normal entries.
Uses reduceOnly for reductions/exits.
Confirms fills via private execution stream.
Reconciles positions periodically using REST.
Cancels stale orders.
Never updates positions from REST ack alone.
```

### Task 12: Monitoring

Implement live dashboard/logs:

```text
current equity
gross/net exposure
positions
orders
fills
latest scores
data gaps
margin usage
daily/weekly/monthly PnL
kill switch status
```

Acceptance:

```text
Writes machine-readable JSONL.
Raises terminal/Telegram/Discord alert hooks if configured.
Kill switch state persists across restart.
```

---

## 17. Unit tests to require

Codex must implement these tests.

```text
test_trade_aggregation_buy_sell_quote:
    Given trades with side Buy/Sell, price, size.
    Verify buy_quote and sell_quote are correct.

test_trade_aggregation_excludes_block_and_rpi:
    Given block/RPI trades and config excludes them.
    Verify ignored for aggression.

test_aggression_signal_positive:
    More buy_quote than sell_quote over EMA window -> positive raw aggression.

test_carry_signal_sign:
    Positive funding -> negative carry signal.
    Negative funding -> positive carry signal.

test_funding_interval_normalization:
    Funding rate with 4h interval is scaled to 8h equivalent correctly.

test_universe_excludes_prelisting:
    isPreListing true -> not tradable.

test_universe_requires_pagination:
    More than 500 symbols are collected through cursor.

test_no_same_bar_leakage:
    Signal timestamp t cannot use returns or trades after t.

test_rounding_qty_step:
    Quantity floors to qtyStep.

test_rounding_price_tick:
    Price rounds to tickSize.

test_no_trade_band:
    Small target diff creates no order.

test_reduce_only_on_exit:
    Any order reducing position sets reduceOnly=true.

test_post_only_default:
    Normal entry order uses timeInForce=PostOnly.

test_risk_daily_loss_kill:
    Daily loss beyond threshold blocks new entries.

test_position_caps:
    No symbol exceeds configured cap.

test_net_exposure_cap:
    Net exposure corrected or scaled down.
```

---

## 18. Research report template

Every backtest run should output:

```text
1. Config hash
2. Date range
3. Symbols included/excluded
4. Signal IC table:
    aggression
    relative volume
    momentum
    carry
    quality
    oi impulse
    composite
5. Quantile spread charts/tables
6. Portfolio results:
    total return
    annualized return
    volatility
    Sharpe-like ratio
    max drawdown
    turnover
    average gross
    average net
7. PnL attribution:
    long book
    short book
    funding
    fees
    slippage
8. Sensitivity:
    base costs
    2x costs
    3x costs
    all-taker
9. Robustness:
    excluding best month
    excluding BTC/ETH/SOL
    high-vol regimes
    low-vol regimes
10. Failure examples:
    worst trades
    largest squeeze losses
    largest funding losses
```

---

## 19. Things not to implement in version 1

Do not implement these until the simple system works:

```text
No ML model.
No reinforcement learning.
No grid trading.
No martingale.
No averaging down without fresh signal.
No new-listing short sleeve.
No bottom-market-cap pump fade sleeve.
No cross-exchange arbitrage.
No portfolio margin mode.
No leverage above 1.5x gross.
No discretionary manual overrides except kill/flatten.
```

Optional future research after version 1:

```text
new-listing decay on Bybit listings
small-cap pump fade with strict size caps
aggression divergence: price up, aggression down
liquidation-event continuation/reversal
funding extreme mean reversion
BTC regime classifier
```

---

## 20. Operational security

For live trading:

```text
Use a Bybit sub-account.
Keep only operational capital on exchange.
Use API key with trading permission only; no withdrawals.
Use IP whitelist where possible.
Store secrets in environment variables or secret manager.
Never log API secrets.
Implement emergency cancel-all and flatten scripts.
Persist kill-switch state.
Back up database.
Monitor API changelog periodically.
```

---

## 21. Final build target

The first complete system should be able to run in three modes:

```bash
python -m src.main collect-data --config config/default.yaml
python -m src.main backtest --config config/default.yaml --start 2025-01-01 --end 2026-04-30
python -m src.main paper --config config/paper.yaml
python -m src.main live --config config/live.yaml
```

Where:

```text
collect-data:
    collects Bybit publicTrade, klines, tickers, funding, open interest.

backtest:
    computes features, validates alphas, runs costed portfolio backtest.

paper:
    computes live targets and simulated fills without real orders.

live:
    trades real Bybit USDT linear perps with all risk controls active.
```

Live mode must refuse to start unless:

```text
config.live.confirm_live_trading == true
API key present
private WebSocket connected
wallet balance fetched
instrument cache fresh
fee rates fetched or conservative fallback enabled
kill switch not active
```

---

## 22. Strategy summary

This is the intended first live version:

```text
Bybit USDT linear perp cross-sectional system.
Main alpha: taker aggression from publicTrade side-of-taker data.
Confirmation: relative quote turnover expansion.
Secondary: short-term momentum and funding carry.
Risk tilt: liquidity/open-interest quality.
Portfolio: long top 20%, short bottom 20%, vol-adjusted, beta-aware.
Execution: post-only maker-biased, no-trade bands, reduce-only exits.
Risk: low gross, strict drawdown stops, data kill switch, squeeze guard.
```

The system should be boring, robust, and measurable. If the standalone aggression signal does not show positive IC and cost-adjusted top-minus-bottom spread on Bybit data, do not proceed to live trading. Fix the data, the assumptions, or the hypothesis first.
