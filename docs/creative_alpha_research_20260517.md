# Creative Alpha Research - 2026-05-17

## Objective

Find creative but defensible improvements to the selected full-PIT Bybit
liquidity-migration short strategy without changing the live/demo path unless a
candidate clears the same PIT, cost, split, drawdown, and execution-realism
standards as the current default.

Current default reference:

```text
liquidity_migration q30 reversal h3 stop 12% take-profit 20% cost 3x
return: +1218.79%
max drawdown: -14.54%
worst 90d return: -5.89%
min split return: +75.64%
avg split Sharpe-like: 2.67
trades: 516
```

## Literature-Inspired Hypotheses

- Crypto returns are often driven by market-specific momentum and attention,
  not traditional macro factors: Liu and Tsyvinski, NBER 24877
  (`https://www.nber.org/papers/w24877`).
- Cross-sectional crypto factors include size, momentum, dollar volume, and
  dollar-volume volatility: Liu, Tsyvinski, and Wu, NBER 25882
  (`https://www.nber.org/papers/w25882`).
- Recent ML evidence says simple signals dominate model complexity; useful
  crypto predictors are price/past alpha/illiquidity/momentum, but gains are
  concentrated in hard-to-trade small, illiquid, volatile coins:
  Cakici et al. (`https://ssrn.com/abstract=4295427`).
- Liquidity-volatility can carry a return premium in crypto:
  Leirvik, Finance Research Letters 2022
  (`https://doi.org/10.1016/j.frl.2021.102031`).
- MAX/salience is ambiguous in crypto. Jia, Liu, and Yan find next-day
  weakness after extreme positive intraday returns
  (`https://doi.org/10.1016/j.frl.2020.101536`), while Li et al. find monthly
  MAX momentum (`https://doi.org/10.1016/j.irfa.2021.101829`). That is a
  warning not to assume equity-style MAX shorting works in perps.

## Implementation Added

Default behavior is unchanged. New disabled-by-default liquidity-migration
research controls were added:

```text
return_7d
close_to_high_7d
close_to_high_30d
prior30_max_daily_return
prior7_return_volatility
intraday_range_1d
```

These support testing momentum, proximity-to-high, MAX/salience,
liquidity/return volatility, and blow-off range filters without touching the
selected demo strategy.

## Full-PIT Runs

Data root:

```text
/Users/jhbvdnsbkvnsd/Desktop/MODEL050426/data/agc-bybit-fullpit-1h-20230503-20260503
```

Reports are under ignored local `data/research_reports/`.

### Existing Event Family Sweep

Report:

```text
data/research_reports/research_20260517_broad_event_families
```

Result:

```text
scenarios: 1152
promotable: 55
promotable families: liquidity_migration only
best headline: +1503.70%, max DD -20.58%, worst 90d -18.08%
```

Decision: reject as default upgrade. The better headline variants are looser
liquidity-migration reversals with materially worse drawdown and 90-day pain.
No other event family survived full-PIT promotion.

### Quant Filter Matrix

Best row from each candidate:

| Candidate | Trades | Return | Max DD | Worst 90d | Min Split | Avg Split Sharpe | Decision |
|---|---:|---:|---:|---:|---:|---:|---|
| 7d momentum >= 10% | 468 | +977.30% | -14.52% | -9.70% | +86.46% | 2.56 | reject: lower return and Sharpe |
| 7d momentum >= 20% | 346 | +403.12% | -18.88% | -9.30% | +40.33% | 1.91 | reject |
| near 7d high | 156 | +183.35% | -5.68% | -4.38% | +36.33% | 3.45 | defensive sleeve only |
| near 30d high | 230 | +274.12% | -12.54% | -5.41% | +45.23% | 3.72 | defensive sleeve only |
| prior30 MAX >= 15% | 159 | +115.49% | -15.38% | -12.44% | +15.86% | 2.52 | reject |
| prior30 MAX >= 25% | 42 | +29.29% | -17.56% | -12.10% | -7.88% | 3.56 | reject: split fail |
| low prior7 return vol | 502 | +962.89% | -20.32% | -16.90% | +104.56% | 2.35 | reject: worse DD |
| high prior7 return vol | 64 | +25.83% | -13.98% | -11.97% | +0.92% | 2.98 | reject |
| intraday range <= 40% | 380 | +404.40% | -13.27% | -10.29% | +52.60% | 2.44 | reject |
| 7d momentum + near high | 216 | +239.56% | -12.54% | -5.00% | +41.75% | 3.61 | defensive sleeve only |
| prior30 MAX + near high | 64 | +54.86% | -8.15% | -5.36% | +14.51% | 4.39 | reject: too sparse |

Strict upgrade count:

```text
return > default AND drawdown no worse AND avg split Sharpe no worse: 0
```

### Defensive Candidate Robustness

Candidate: near 30d high, q35, 2d hold, 12% stop, 20% take profit.

```text
1x cost:  +309.95%, max DD -12.26%, min split +49.15%
3x cost:  +274.12%, max DD -12.54%, min split +45.23%
2h delay: +126.88%, max DD -14.14%, min split +22.90%
6h delay:  +86.74%, max DD -14.48%, min split +21.75%
```

Decision: do not promote. It is cost robust but very delay sensitive and gives
up too much return. It can stay as a future low-drawdown sleeve candidate if
the objective changes from maximizing compounding to reducing drawdown.

## Decision

Do not change the selected demo strategy.

The current full-PIT liquidity-migration short remains the strongest evidence.
The creative alpha pass found useful diagnostics and one possible defensive
sleeve, but no candidate is good enough to replace the default without either
raising drawdown, reducing execution robustness, or sacrificing too much return.

Next research should focus on genuinely orthogonal data, not more daily OHLCV
filters on the same liquidity-migration event. Best candidates:

```text
perp funding history
open interest migration
order-book imbalance / taker aggression around the 1h entry
borrow/funding crowding proxy
stop-fill realism using intrabar high/low stress
```

## Orthogonal Funding/OI Pass

This pass added the next research layer requested after the first creative
alpha sweep:

```text
funding
open interest
taker-flow imbalance when historical public trades are present
adverse stop-fill realism
```

Primary venue references:

- Bybit funding history:
  `https://bybit-exchange.github.io/docs/v5/market/history-fund-rate`
- Bybit open interest:
  `https://bybit-exchange.github.io/docs/v5/market/open-interest`
- Bybit recent public trades:
  `https://bybit-exchange.github.io/docs/v5/market/recent-trade`
- Bybit orderbook:
  `https://bybit-exchange.github.io/docs/v5/market/orderbook`

The orderbook endpoint is a current snapshot endpoint, not historical PIT data.
It is therefore not used in the 2023-2026 backtest. Using today's book against
old trades would be lookahead. Historical taker imbalance is wired through
`signed_flow_1h`, but this full-PIT root does not currently have signed-flow
archives loaded.

Quant inspiration:

- Perpetual futures returns decompose into basis/funding, price-volume, size,
  liquidity, momentum, and volatility predictors:
  `https://www.research.ed.ac.uk/en/publications/anatomy-of-cryptocurrency-perpetual-futures-returns/`
- Funding rates are forecastable but time-varying:
  `https://ssrn.com/abstract=5576424`
- Open interest in perpetual swaps can be noisy or exchange-dependent, so OI
  needs coverage and robustness checks instead of blind trust:
  `https://arxiv.org/abs/2310.14973`

### Implementation Added

Default behavior remains unchanged. New disabled-by-default research controls:

```text
stop_fill_mode = stop | bar_extreme
liquidity_migration_funding_rate_last_min/max
liquidity_migration_funding_3d_sum_min/max
liquidity_migration_funding_7d_sum_min/max
liquidity_migration_open_interest_return_3d_min/max
liquidity_migration_open_interest_return_7d_min/max
liquidity_migration_volume_to_oi_quote_min/max
liquidity_migration_taker_imbalance_1d_min/max
liquidity_migration_taker_imbalance_3d_min/max
```

Backtest funding coverage was tightened: a partially downloaded funding dataset
no longer silently marks missing symbols/dates as modeled zero funding.

### Orthogonal Data Coverage

Data root:

```text
/Users/jhbvdnsbkvnsd/Desktop/MODEL050426/data/agc-bybit-fullpit-1h-20230503-20260503
```

Downloaded via Bybit V5:

| Dataset | Rows | Symbols | Notes |
|---|---:|---:|---|
| funding | 1,428,114 | 465/465 | all manifest symbols have rows |
| open_interest daily | 285,587 | 314/465 | 151 symbols missing OI |
| signed_flow_1h | 0 | 0 | not available in this root |

Because OI is incomplete, OI filters are only valid when they naturally require
OI coverage. They are not valid as universal market-wide signals.

### Funded Default Rerun

Report:

```text
data/research_reports/research_20260517_default_with_funding_oi_features
```

Result:

```text
return: +957.82%
max drawdown: -18.62%
worst 90d return: -7.00%
min split return: +77.60%
avg split Sharpe-like: 2.49
funding return: -22.03%
funding mode: partial, because 2/516 trades lack full funding coverage
trades: 516
```

Interpretation: still strong after real funding, but worse than the earlier
fee/slippage-only +1218.79% headline. Funding is a real drag for this short
strategy because many shorts were held while funding was negative.

### Adverse Stop-Fill Stress

Report:

```text
data/research_reports/research_20260517_default_funding_bar_extreme_stops
```

Stress assumption: when a stop is touched inside an hourly bar, fill at the
adverse hourly extreme instead of exactly at the stop.

Result:

```text
return: +141.34%
max drawdown: -35.88%
worst 90d return: -31.24%
min split return: -13.69%
OOS 2025-2026 return: -13.69%
promotion: fail
```

This is the most important negative result in the audit. The default strategy
is profitable under exact stop fills, but it is not robust enough under a harsh
hourly stop-fill model.

### Orthogonal Sweep

Report:

```text
data/research_reports/research_20260517_orthogonal_funding_oi_sweep
```

Best interpretable rows:

| Candidate | Stop Fill | Trades | Return | Max DD | Worst 90d | Min Split | Funding | Decision |
|---|---|---:|---:|---:|---:|---:|---:|---|
| baseline | stop | 516 | +957.82% | -18.62% | -7.00% | +77.60% | -22.03% | strong but stop-stress fragile |
| baseline | bar_extreme | 516 | +141.34% | -35.88% | -31.24% | -13.69% | -22.03% | reject |
| funding 7d sum >= 0 | stop | 273 | +431.73% | -11.92% | -9.47% | +35.35% | +3.13% | pass |
| funding 7d sum >= 0 | bar_extreme | 273 | +213.66% | -16.61% | -15.59% | +12.10% | +3.13% | pass |
| funding last >= 0 | bar_extreme | 260 | +193.74% | -16.61% | n/a | +9.30% | n/a | pass but weaker |
| avoid 7d funding < -20 bps | bar_extreme | 308 | +249.90% | -18.61% | n/a | +2.78% | n/a | pass but thin split margin |
| OI required | bar_extreme | 309 | +16.52% | -37.59% | n/a | -26.41% | n/a | reject |
| OI 3d expansion >= 10% | bar_extreme | 291 | +19.03% | -33.41% | n/a | -16.99% | n/a | reject |
| funding 7d >= 0 + OI 3d expansion | bar_extreme | 125 | +62.87% | -14.38% | n/a | +4.18% | n/a | defensive only |

Full values are in:

```text
orthogonal_alpha_sweep_summary.csv
orthogonal_alpha_sweep_trades.csv
orthogonal_alpha_sweep_coverage.json
```

### Candidate Decision

The first genuinely better research candidate is:

```text
liquidity_migration short
funding_7d_sum >= 0 at signal close
3d max hold
12% stop
20% take profit
3x costs
125% gross exposure
6 max active symbols
```

Why it is better:

- It cuts trades from 516 to 273.
- It converts funding from -22.03% drag to +3.13% tailwind.
- It survives adverse hourly stop-fill stress:
  +213.66%, -16.61% max DD, +12.10% min split.
- It has lower headline return than the default under exact fills, but the
  default fails the harsher execution realism test.

Why it is not yet deployed:

- It has no forward/demo evidence yet.
- The demo stack does not currently compute the 7d funding sum gate.
- OI and taker-flow did not produce a stronger standalone promotion candidate.

Decision: promote `funding_7d_sum >= 0` to the next shadow/demo research
candidate, not to live default execution yet. The active demo strategy should
not be changed until the demo cycle can compute the same funding feature and a
forward sample confirms the edge is still present.
