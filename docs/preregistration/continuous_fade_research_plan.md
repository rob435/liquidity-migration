# Continuous Short-Fade Research Plan

## Context

Core interpretation:

```text
The strategy is credible enough to take seriously.
It is not yet credible enough to trust blindly with meaningful live capital.
```

The immediate research goal is not to find another parameter tweak. The goal is to determine whether the edge is real, whether the BTC regime filter is robust, whether the entry timing is already close to optimal, and whether the strategy is implicitly selling uncapped short-tail risk.

---

# 1. Primary Research Objective

## Main question

Can the continuous short-fade strategy survive live deployment after realistic execution, adverse price paths, exchange failure modes, and crypto squeeze tails?

## Secondary questions

```text
1. Is the entry signal real?
2. Is the BTC regime filter real or overfit?
3. Is the 24h hold / 12% TP structure real?
4. Is no normal stop-loss rational?
5. Is TWAP bad because delay kills alpha, or because the implementation is wrong?
6. Are losses controlled by truly small sizing, or merely hidden by a benign sample?
7. Are the worst trades avoidable before entry?
8. Are the worst trades controllable after entry?
9. Is the backtest execution realistic?
10. Is the live architecture safe under failure?
```

Do not collapse these questions into one Sharpe or MAR number.

---

# 2. Working Hypothesis

Current best hypothesis:

```text
There is likely a real short-term mean-reversion edge.

The strategy probably enters before local exhaustion, so normal adverse excursion is part of the edge.

Tight stops are bad because they cut normal winner paths.

TWAP is bad because it delays exposure, misses fast reversals, and biases full-size fills toward persistent squeezes.

The BTC regime filter is probably a legitimate risk-regime filter, but it may also be the largest overfit surface.

The biggest unresolved risk is not average trade quality.
The biggest unresolved risk is extreme right-tail price movement while short.
```

Therefore, the research should not obsess over slightly better average entry. It should map the trade path and determine which pain is normal versus which pain is structural failure.

---

# 3. Non-Negotiable Measurement Layer

Before running more strategy experiments, create a canonical research dataset. Every signal, trade, skipped signal, hypothetical fill, and live order outcome should become analyzable.

## 3.1 Required Signal Table

One row per signal, including signals that did not become trades.

```text
signal_id
symbol
signal_ts
signal_bar_close_ts
entry_eligible_ts
profile_name
profile_hash
git_commit
component_id
component_score
composite_score
residual_momentum_rank
residual_momentum_value
feature_max_ret168
liquidity_value
liquidity_rank
spread_bps
depth_10bps
depth_25bps
depth_50bps
volume_1h
volume_24h
volume_zscore
funding_rate
open_interest
open_interest_1h_change
open_interest_24h_change
btc_1h_return
btc_4h_return
btc_24h_return
btc_30d_regime_value
btc_regime_on_off
eth_24h_return
sector_proxy_return
market_volatility_state
symbol_age_days
listing_age_days
time_of_day_utc
day_of_week
weekend_flag
```

Important: record unfilled and skipped signals. If only executed trades are studied, timing tests become biased.

## 3.2 Required Trade Table

One row per executed trade.

```text
trade_id
signal_id
symbol
side
entry_ts
entry_price
entry_qty
entry_notional
entry_fee
entry_slippage_bps
entry_order_type
entry_fill_type
maker_taker
exit_ts
exit_price
exit_reason
exit_fee
exit_slippage_bps
gross_pnl
net_pnl
funding_pnl
borrow_or_carry_pnl
max_adverse_excursion_pct
max_favorable_excursion_pct
time_to_mae
time_to_mfe
time_to_first_profit
time_underwater
time_to_recovery
max_margin_usage
liquidation_distance_min
btc_return_during_trade
symbol_return_during_trade
component_id
```

## 3.3 Required Forward Path Table

For every signal, save forward path snapshots.

```text
signal_id
symbol
t_plus_15m_return
t_plus_30m_return
t_plus_1h_return
t_plus_2h_return
t_plus_3h_return
t_plus_4h_return
t_plus_6h_return
t_plus_8h_return
t_plus_12h_return
t_plus_24h_return
max_up_1h
max_up_2h
max_up_4h
max_up_6h
max_up_12h
max_up_24h
max_down_1h
max_down_2h
max_down_4h
max_down_6h
max_down_12h
max_down_24h
volume_path
oi_path
funding_path
btc_path
spread_path
depth_path
```

This table answers whether timing and stops are actually improvable.

---

# 4. Validation Standards

Do not treat Sharpe 3.4 and MAR 10 as final proof. Treat them as candidate evidence.

Validate all results using:

```text
net of fees
net of realistic slippage
net of funding
active-period Sharpe
trade-cluster-adjusted Sharpe
year-by-year MAR
component-level attribution
regime-level attribution
liquidity-bucket attribution
symbol-concentration attribution
worst-trade dependence
worst-week dependence
synthetic squeeze robustness
parameter-surface robustness
```

References worth using in research notes:

```text
Deflated Sharpe Ratio:
Bailey & Lopez de Prado, "The Deflated Sharpe Ratio"
https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf

Probability of Backtest Overfitting:
Bailey et al., "The Probability of Backtest Overfitting"
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253

Sharpe inference under serial correlation:
Andrew Lo, "The Statistics of Sharpe Ratios"
https://ideas.repec.org/a/taf/ufajxx/v58y2002i4p36-52.html
```

---

# 5. Phase 0 — Freeze the Current Baseline

Before experimenting, freeze the exact current strategy.

## 5.1 Create a canonical baseline

Suggested name:

```text
continuous_ensemble_v2_baseline_2026_06
```

Freeze:

```text
BTC regime filter
residual quantile
component weights
liquidity floor
TP level
max hold
sizing formula
cooldowns
sniper behavior
entry delay behavior
exit behavior
hedge behavior
fee assumptions
slippage assumptions
funding assumptions
universe construction
data vendor/version
```

Every future test compares to this frozen baseline.

## 5.2 Baseline report

Generate a locked report with:

```text
total trades
total signals
executed signal rate
win rate
avg win
avg loss
payoff ratio
profit factor
expectancy per trade
expectancy per signal
median PnL
mean PnL
skew
kurtosis
Sharpe
Sortino
Calmar/MAR
max drawdown
worst trade
worst day
worst week
worst month
average hold
median hold
average MAE
median MAE
average MFE
median MFE
average time to MAE
average time to first profit
average time underwater
```

Report performance on four bases:

```text
calendar-time basis
active-time basis
trade basis
cluster basis
```

Cluster basis matters because 900 trades may represent far fewer independent market events.

---

# 6. Phase 1 — Prove the Backtest Is Not Lying

## 6.1 Closed-bar / lookahead audit

For every feature, verify:

```text
feature timestamp <= signal decision timestamp
entry decision uses only closed bars
daily features do not include incomplete future daily data
universe selection does not know future listings
liquidity filter uses only past data
funding data timestamp is tradable at the decision time
open interest data timestamp is tradable at the decision time
BTC regime value is lagged correctly
```

Add automated assertions:

```python
assert feature_ts <= signal_ts
assert bar_close_ts <= decision_ts
assert entry_ts >= signal_bar_close_ts
assert universe_snapshot_ts <= signal_ts
```

If any feature violates this, assume the Sharpe is contaminated until proven otherwise.

## 6.2 Listing and survival bias audit

For every historical date, reconstruct:

```text
which symbols existed then
which symbols were tradable then
which symbols had enough historical bars then
which symbols were delisted later
which symbols were suspended
which symbols were too illiquid
```

Do not let the current universe leak into the past.

Specific test:

```text
Run backtest using only symbols known tradable at each historical timestamp.
Compare to backtest using current symbol list.
```

If the current-list backtest is materially better, there is survival/listing bias.

## 6.3 Cost realism audit

Run baseline under multiple cost models:

```text
optimistic maker/taker as coded
all taker entries
all taker exits
entry maker / exit taker
+2 bps slippage
+5 bps slippage
+10 bps slippage
depth-based slippage
emergency-exit slippage
funding included
funding excluded
funding stressed 2x
```

The strategy should not require perfect maker fills.

## 6.4 Reconciliation realism audit

Simulate ugly live cases:

```text
partial fill
no fill
delayed fill report
duplicate orderLinkId
order accepted but REST timeout
order rejected after preflight row
PostOnly cancel
TP filled but ledger stale
position exists but trade row missing
risk service sees position before strategy cycle sees it
```

Each case should have deterministic state-machine recovery.

---

# 7. Phase 2 — Understand the Trade Path

This phase makes timing and stops tractable.

## 7.1 MAE/MFE table

For all 900 trades, compute:

```text
MAE = maximum adverse price move before exit
MFE = maximum favorable price move before exit
```

For shorts:

```text
MAE = max_price_during_trade / entry_price - 1
MFE = 1 - min_price_during_trade / entry_price
```

Create this table:

| MAE bucket | Trades | Win rate | Avg net PnL | Median net PnL | Profit factor | Avg recovery time | 5% tail | Worst trade |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0–1% | | | | | | | | |
| 1–2% | | | | | | | | |
| 2–5% | | | | | | | | |
| 5–10% | | | | | | | | |
| 10–20% | | | | | | | | |
| 20–40% | | | | | | | | |
| 40–80% | | | | | | | | |
| 80%+ | | | | | | | | |

Critical derived metrics:

```text
P(final PnL > 0 | MAE >= x)
E[final PnL | MAE >= x]
ES_95[final PnL | MAE >= x]
median time to recovery | MAE >= x
```

This tells you where normal pain becomes abnormal.

## 7.2 Winner MAE vs loser MAE

Create overlapping distributions:

```text
MAE of winners
MAE of losers
MFE of winners
MFE of losers
time_to_MAE of winners
time_to_MAE of losers
time_underwater of winners
time_underwater of losers
```

Interpretation:

```text
If winner MAE and loser MAE heavily overlap:
    price stops will be bad.

If losers separate after a clear MAE threshold:
    catastrophic stops can help.

If losers separate by time underwater:
    time-underwater exits can help.

If losers separate by signal deterioration:
    signal-invalidation exits can help.
```

## 7.3 Event-time path curves

For every signal, align time to signal timestamp.

Compute average and median forward path:

```text
0m
15m
30m
1h
2h
3h
4h
6h
8h
12h
18h
24h
36h
48h
```

Do this separately for:

```text
all signals
executed trades
winners
losers
top 10% winners
bottom 10% losers
each component
each BTC regime
each liquidity bucket
each volume-z bucket
high funding vs low funding
high OI growth vs low OI growth
weekend vs weekday
```

Path types to identify:

```text
A. Immediate reversal
B. Squeeze then reversal
C. Slow drift down
D. Continued squeeze / failed fade
E. No edge / noise
```

## 7.4 Trade path labels

Assign each historical signal a path label.

Example labels:

```text
FAST_REVERT:
    goes profitable within 1h and reaches TP or positive exit

SQUEEZE_REVERT:
    first moves against by > X, then exits profitable

SLOW_REVERT:
    does not profit quickly but eventually mean-reverts

CHOP:
    never large adverse or favorable excursion

FAILED_FADE:
    moves against materially and does not recover before max-hold

DISASTER:
    extreme adverse move beyond catastrophic threshold
```

Then try to predict path label from pre-entry features.

This matters more than predicting exact PnL.

---

# 8. Phase 3 — Timing Research

TWAP worsening results is useful information. Replace blunt TWAP with path-aware timing.

## 8.1 Timing test principle

Judge timing by:

```text
PnL per original signal
```

not only:

```text
PnL per filled trade
```

A delayed entry can look excellent per filled trade while missing fast winners and reducing total expectancy.

Every timing test must report:

```text
signals eligible
signals filled
fill rate
average entry improvement
average PnL per signal
average PnL per filled trade
median PnL per signal
drawdown
worst trade
MAE
MFE
time underwater
turnover
fees
capacity
```

## 8.2 Simple delay grid

Run these entry variants:

```text
immediate baseline
15m delay
30m delay
1h delay
2h delay
3h delay
4h delay
6h delay
next closed red 15m candle
next closed red 1h candle
next failed high
next lower high
wait for residual momentum deceleration
wait for volume deceleration
wait for OI deceleration
```

Report:

| Entry rule | Fill rate | PnL/signal | PnL/fill | Sharpe | MAR | Max DD | Worst trade | Avg MAE | Median time to profit |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Immediate | | | | | | | | | |
| 15m delay | | | | | | | | | |
| 30m delay | | | | | | | | | |
| 1h delay | | | | | | | | | |
| 2h delay | | | | | | | | | |
| 4h delay | | | | | | | | | |
| Failed high | | | | | | | | | |
| Volume deceleration | | | | | | | | | |
| OI deceleration | | | | | | | | | |

Expected interpretation:

```text
If immediate dominates:
    alpha half-life is short.

If small delays help only in certain buckets:
    use conditional timing.

If waiting for confirmation improves tail but kills expectancy:
    use it only as a skip/invalidation filter, not as the main entry trigger.
```

## 8.3 Adverse-limit entry tests

Instead of TWAP, test entry only if price moves against the signal first.

For shorts:

```text
enter at signal close
enter at signal close + 0.5%
enter at signal close + 1.0%
enter at signal close + 1.5%
enter at signal close + 2.0%
enter at signal close + 0.5 ATR
enter at signal close + 1.0 ATR
enter at signal close + 1.5 ATR
enter at signal close + 1σ 1h vol
enter at signal close + 2σ 1h vol
```

Report both:

```text
filled-trade expectancy
missed-signal opportunity cost
drawdown improvement
tail improvement
```

This answers whether better price is worth missed reversals.

## 8.4 Conditional scale-in tests

This is likely more promising than TWAP.

Test structures:

```text
Variant A:
    100% immediate

Variant B:
    70% immediate
    30% add at +1 ATR adverse

Variant C:
    50% immediate
    25% add at +1 ATR adverse
    25% add at +2 ATR adverse

Variant D:
    50% immediate
    50% add only after failed high

Variant E:
    60% immediate
    20% add at +1%
    20% add at +2%
```

Strict no-add rules:

```text
do not add if residual momentum strengthens
do not add if volume accelerates
do not add if OI expands aggressively
do not add if BTC 1h/4h trend turns hostile
do not add if spread widens beyond threshold
do not add after max signal age
do not add if portfolio heat > cap
do not add if symbol has already exceeded adverse threshold
```

The add should be earned, not automatic.

## 8.5 TWAP post-mortem

Do not discard the TWAP experiment until you decompose why it failed.

Break TWAP trades into:

```text
first clip PnL
second clip PnL
third clip PnL
fourth clip PnL
full TWAP PnL
missed fast-reversal PnL
extra exposure during failed fades
```

Key diagnostic:

```text
Are later TWAP clips lower expectancy than first clips?
```

If yes, TWAP is adding into decayed alpha or continuation cases.

Also compute:

```text
TWAP full-size fill rate on winners
TWAP full-size fill rate on losers
```

If losers get full size more often than winners, TWAP is structurally toxic.

## 8.6 Entry quality decomposition

For each timing variant, decompose result into:

```text
entry_price_effect
fill_rate_effect
signal_decay_effect
tail_effect
fee_effect
slippage_effect
```

Example conclusion format:

```text
TWAP improved average entry by +0.6%,
but reduced fill rate by 35%,
and increased exposure to failed fades by 22%.
Net expectancy worsened.
```

---

# 9. Phase 4 — Stop-Loss and Loss-Containment Research

Normal stops worsening performance is plausible and not necessarily bad.

The correct question is:

```text
Can we add disaster protection without destroying the edge?
```

## 9.1 Separate stop types

Do not test “stop-loss” as one thing. Test five mechanisms separately.

```text
1. Tactical price stop
2. Catastrophic price stop
3. Time-underwater stop
4. Signal-invalidation stop
5. Portfolio heat / drawdown stop
```

The strategy likely should avoid type 1 and use types 2–5.

## 9.2 Tactical stop frontier

Run this not to select a tight stop, but to understand the shape.

Fixed adverse stops:

```text
5%
7.5%
10%
12.5%
15%
20%
30%
40%
60%
80%
no stop
```

Volatility stops:

```text
1σ
2σ
3σ
4σ
5σ
6σ
8σ
10σ
```

ATR stops:

```text
1 ATR
2 ATR
3 ATR
4 ATR
5 ATR
6 ATR
8 ATR
10 ATR
```

For each, report:

```text
net PnL
Sharpe
MAR
max DD
expected shortfall
worst trade
win rate
profit factor
average winner cut short
average loser reduced
number of stops hit
post-stop reversion rate
```

Critical metric:

```text
P(price later hits original TP after stop-out)
```

If this is high, the stop is inside normal strategy noise.

## 9.3 Catastrophic stop design

A catastrophic stop should not be optimized to maximize backtest MAR. It should be designed to prevent unacceptable damage.

Candidate formula:

```text
cat_stop_pct = max(
    historical_95th_percentile_winner_MAE,
    4 × recent_24h_realized_vol,
    3 × ATR_24h,
    fixed_min_pct
)
```

Hard clamps:

```text
min_cat_stop = 20%
max_cat_stop = 80%
```

Then size from catastrophic loss:

```text
max_trade_loss_usd = equity × trade_loss_budget
position_notional <= max_trade_loss_usd / cat_stop_pct
```

Example:

```text
equity = $100,000
trade_loss_budget = 0.10% = $100
cat_stop = 40%
max_notional = $100 / 0.40 = $250
```

This looks tiny. That is the point. If the strategy avoids tight stops, sizing must carry the burden.

## 9.4 Disaster stop placement

Even if the stop is very wide, it should exist as venue-side or reduce-only conditional protection.

Research variants:

```text
venue position-level disaster stop
reduce-only conditional stop order
risk-daemon synthetic disaster stop
venue stop + synthetic monitor
partial disaster stop
full disaster stop
```

Preferred live design:

```text
venue disaster stop confirmed
risk daemon monitors same threshold
strategy blocks new entries if venue protection missing
```

## 9.5 Time-underwater exits

Because trades normally move against you before reverting, price-only stops may fail. Time-underwater may work better.

Test rules:

```text
exit if MAE > 5% and age > 6h and not recovered
exit if MAE > 10% and age > 6h and not recovered
exit if MAE > 10% and age > 12h and residual still extreme
exit if unrealized PnL < -X and time_to_first_profit > N
exit if still underwater after 75% of max_hold
```

Report:

```text
number of exits
average loss avoided
average winner sacrificed
post-exit reversion rate
drawdown improvement
MAR impact
tail improvement
```

Time-underwater is promising if losers are defined more by “failed to recover” than by “went red.”

## 9.6 Signal-invalidation exits

Stop because the reason for the trade is invalid, not because price moved.

Test invalidation features:

```text
residual momentum continues increasing
symbol makes fresh high after entry
1h/4h volume acceleration continues
open interest expands while price rises
funding becomes more positive
BTC 1h/4h trend turns hostile
sector basket rises
spread widens / depth disappears
price remains above breakout level
no lower high formed after N bars
```

Example rule:

```text
exit or reduce if:
    trade_age >= 3h
    and unrealized_pnl < 0
    and residual_momentum_rank is still in extreme decile
    and volume_zscore is rising
    and open_interest_change_4h > threshold
```

This type of exit is more likely to improve live risk than a dumb 10% stop.

## 9.7 Portfolio-level stops

Even if individual stops hurt, portfolio-level kill switches are required.

Research these:

```text
rolling_1h_loss_limit
rolling_4h_loss_limit
rolling_24h_loss_limit
rolling_7d_loss_limit
max_open_short_notional
max_symbol_notional
max_sector_or_theme_notional
max_same_signal_cluster_notional
max_margin_usage
max_unrealized_drawdown
max_unprotected_position_age
max_reconciliation_error
max_WS_disconnect_age
```

Possible policy:

```text
Level 1:
    rolling 1h loss > 0.5% equity
    block new entries

Level 2:
    rolling 24h loss > 1.0% equity
    block new entries and cancel adds/snipers

Level 3:
    rolling 24h loss > 1.5% equity
    reduce worst positions by 50%

Level 4:
    rolling 24h loss > 2.0% equity
    flatten all non-hedge shorts
```

Exact thresholds depend on account size and trade sizing. The structure matters more than the first threshold values.

---

# 10. Phase 5 — BTC Regime Filter Research

Given:

```text
filter on: MAR ~10
filter off: MAR ~2
```

The base alpha may be real, but the filter is a major driver of capital efficiency and drawdown control.

## 10.1 Robustness grid

Test BTC return lookbacks:

```text
10d
15d
20d
25d
30d
35d
40d
50d
60d
90d
```

Test signal types:

```text
simple return
EMA slope
price vs moving average
realized-vol-adjusted return
trend + volatility state
drawdown from high
breakout/chop classifier
```

Test regime actions:

```text
binary on/off
linear size scaling
nonlinear size scaling
TP adjustment
max-hold adjustment
entry threshold adjustment
```

Interpretation:

```text
If only 30d works:
    bad; likely overfit.

If 20d–60d broadly works:
    good; likely robust regime effect.
```

## 10.2 Regime delay test

Live regime filters are lagged. Test:

```text
no delay
1h delay
4h delay
12h delay
24h delay
48h delay
```

If performance collapses with a 24h delay, the filter may be too timing-sensitive.

## 10.3 Regime perturbation test

Add noise:

```text
randomly flip 5% of regime classifications
randomly flip 10% of regime classifications
randomly delay regime flips
shift regime boundary by ±5 days
shift regime boundary by ±10 days
```

The strategy should degrade gracefully.

## 10.4 Binary filter vs scaling

A binary filter causes sparse trades and sample-size issues.

Test size scaling:

```text
if BTC regime strongly favorable:
    100% size

if mildly favorable:
    50% size

if neutral:
    20% size

if hostile:
    0% size
```

Compare to binary on/off:

```text
Sharpe
MAR
trade count
drawdown
tail exposure
monthly smoothness
```

A scaling regime may reduce overfit and increase sample size.

---

# 11. Phase 6 — Feature and Component Ablation

The ensemble must justify every component.

## 11.1 Component report

For each component:

```text
number of signals
number of trades
net PnL
PnL per signal
PnL per trade
Sharpe
MAR
max drawdown
win rate
avg MAE
worst trade
correlation to other components
performance by BTC regime
performance by liquidity bucket
performance by year
```

## 11.2 Leave-one-out tests

Run:

```text
baseline
remove component A
remove component B
remove component C
only component A
only component B
only component C
equal-weight components
optimized weights
randomized weights
```

Interpretation:

```text
If optimized weights outperform equal weights only slightly:
    use equal weights.

If one component dominates:
    simplify.

If a component helps Sharpe but worsens tail materially:
    cap or exclude it.
```

## 11.3 Parameter sensitivity

Test broad ranges, not tiny tweaks.

```text
residual quantile:
    0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40

TP:
    4%, 6%, 8%, 10%, 12%, 14%, 16%, 20%

max hold:
    6h, 12h, 18h, 24h, 36h, 48h

liquidity floor:
    $250k, $500k, $1m, $2m, $5m

cooldown:
    0h, 6h, 12h, 24h, 48h, 7d

component trigger threshold:
    wide grid
```

Do not select the best cell. Look for stable plateaus.

Good surface:

```text
many nearby configs work
performance degrades smoothly
same sign across years/regimes
```

Bad surface:

```text
one magical config works
nearby configs collapse
performance concentrated in one quarter
```

---

# 12. Phase 7 — Worst-Trade and Tail-Risk Research

For short perps, this may matter more than Sharpe.

## 12.1 Worst-trade dependency

Run:

```text
baseline
remove best 1 trade
remove best 5 trades
remove best 10 trades
remove worst 1 trade
remove worst 5 trades
remove worst 10 trades
double worst 1 trade
double worst 5 trades
replace worst trade with +100% adverse squeeze
replace worst 3 trades with +100% adverse squeeze
```

Report impact on:

```text
total PnL
Sharpe
MAR
max DD
time to recover
risk of ruin
```

If one or two synthetic events destroy the strategy, sizing is too large or disaster protection is too weak.

## 12.2 Synthetic squeeze injection

Inject these events at random times and at worst possible times:

```text
one held coin +30% instantly
one held coin +50% instantly
one held coin +100% instantly
one held coin +200% instantly
three held coins +30% together
three held coins +50% together
five held coins +30% together
BTC +8% in 4h
BTC +12% in 24h
sector basket +20%
exchange outage for 30m during squeeze
exchange outage for 2h during squeeze
risk daemon down during squeeze
REST timeout after stop trigger
```

Measure:

```text
equity drawdown
margin usage
liquidation distance
positions liquidated
time to flatten
hedge offset
post-event MAR
days to recovery
```

This answers whether small sizing is actually small enough.

## 12.3 Risk-of-ruin simulation

Use empirical trade returns plus injected tails.

Monte Carlo resampling units should be clusters, not individual trades.

Simulate:

```text
1,000 paths
10,000 paths
cluster bootstrap
block bootstrap
regime bootstrap
worst-cluster overweighted
tail-injected bootstrap
```

Output:

```text
median annual return
5th percentile annual return
1st percentile annual return
expected shortfall
probability of 5% drawdown
probability of 10% drawdown
probability of 20% drawdown
probability of account impairment
longest flat period
longest drawdown
```

---

# 13. Phase 8 — Execution Research

The strategy is path-sensitive, so execution assumptions matter.

## 13.1 Fill model study

For every order type, estimate:

```text
fill probability
time to fill
partial-fill probability
adverse selection after fill
cancel rate
PostOnly cancel rate
maker/taker split
price improvement
slippage
missed trade cost
```

For PostOnly sniper entries, specifically measure:

```text
posted price distance
fill rate
fill-to-profit probability
fill-to-loss probability
PnL after filled maker order
PnL after missed maker order
adverse selection after passive fill
```

Passive fills in short mean-reversion can be dangerous because you get filled when price is moving against you.

## 13.2 Entry method comparison

Test:

```text
market entry
aggressive limit
passive limit
PostOnly at touch
PostOnly above touch
sniper ladder
conditional scale-in ladder
hybrid: market initial + passive add
hybrid: passive initial + market fallback
```

Report:

```text
net PnL
fill rate
fee impact
slippage
missed winners
extra losers
tail risk
drawdown
```

## 13.3 Exit execution

TP exits are easy in backtest. Disaster exits are not.

Model:

```text
TP maker fill
TP taker fill
stop-market slippage
stop-limit non-fill
reduce-only reject
partial stop fill
conditional order trigger delay
order book gap
```

For disaster stops, assume exits are taker and slippage is bad.

---

# 14. Phase 9 — Sizing Research

If no tight stop is used, sizing is the real stop.

## 14.1 Current sizing decomposition

For each trade, log:

```text
base size
inverse-vol multiplier
BTC-risk multiplier
component weight
liquidity cap
portfolio cap
final notional
notional / equity
notional / 24h volume
notional / 1h volume
notional / top-book depth
loss if +20%
loss if +50%
loss if +100%
loss if +200%
```

## 14.2 Loss-at-disaster sizing

Add a shadow sizing model:

```text
catastrophic_move_pct = max(
    fixed_cat_pct,
    N × realized_vol,
    N × ATR,
    historical_tail_move
)

max_notional = max_loss_usd / catastrophic_move_pct
```

Compare current size to disaster-based size:

```text
current_notional / disaster_safe_notional
```

If this ratio is > 1 for many trades, current sizing is implicitly assuming no disaster.

## 14.3 Portfolio heat

Define:

```text
portfolio_heat = sum over positions(loss_if_catastrophic_move)
```

Example:

```text
for each short:
    loss_if_50pct_up = notional × 50%

portfolio_heat_50pct = sum(loss_if_50pct_up)
```

Set caps:

```text
max portfolio heat under +50% single-name shock
max portfolio heat under +30% correlated shock
max portfolio heat under +100% single-name shock
```

This is more useful than gross exposure alone.

---

# 15. Phase 10 — Skip Logic

This may be the highest ROI area.

Since timing and stops both worsened, the improvement may be:

```text
take the good trades immediately
skip the worst 5–15%
```

## 15.1 Build loser taxonomy

For bottom-decile trades, manually and statistically classify:

```text
BTC rip
sector rip
symbol-specific pump
listing/news-like event
low-liquidity squeeze
funding/OI squeeze
volume continuation
signal too early
failed data quality
execution/slippage problem
```

## 15.2 Pre-entry predictors of bad trades

Test features:

```text
symbol 1h return
symbol 4h return
symbol 24h return
symbol 7d return
distance from 7d high
distance from 30d high
volume_zscore
volume acceleration
open interest acceleration
funding level
funding change
spread_bps
depth collapse
BTC 1h return
BTC 4h return
BTC volatility
ETH return
sector return
listing age
market cap proxy / liquidity rank
time of day
day of week
recent number of signals
portfolio crowding
```

Use simple models first:

```text
univariate buckets
two-way buckets
decision stump
logistic regression
shallow tree
monotonic scorecard
```

Avoid complex ML until the simple version works.

## 15.3 Bad-trade classifier target

Do not train directly on profit. Train on path failure.

Possible targets:

```text
MAE > 20%
MAE > 40%
trade never becomes profitable
trade underwater for > 12h
bottom 10% net PnL
failed fade label
```

This is more stable than predicting exact return.

## 15.4 Skip rule design

Example:

```text
skip if:
    volume_zscore > high_threshold
    and open_interest_4h_change > threshold
    and BTC_4h_return > threshold
    and residual_momentum still accelerating
```

Goal:

```text
remove 5–15% of trades
reduce worst-tail losses by 25–50%
sacrifice less than 10–20% of gross PnL
```

If a skip rule removes 30% of trades to improve MAR, it may be overfit.

---

# 16. Phase 11 — Hedge Research

The BTC/ETH hedge is useful but probably too blunt.

## 16.1 Attribution

Separate:

```text
short-alpha PnL
hedge PnL
funding PnL
execution PnL
residual unexplained PnL
```

For every trade cluster:

```text
short book loss
predicted BTC/ETH beta loss
actual hedge offset
residual idiosyncratic loss
```

## 16.2 Hedge frequency

Test:

```text
daily rebalance
12h rebalance
6h rebalance
threshold rebalance when gross short changes > 10%
threshold rebalance when beta exposure changes > X
threshold rebalance when BTC moves > Y
threshold rebalance when drawdown > Z
```

Daily hedge may be too slow if the short book changes materially intraday.

## 16.3 Hedge failure modes

Simulate:

```text
short book shrinks but hedge remains
short book grows but hedge stale
ETH leg unmanaged
BTC/ETH correlation breaks
alt basket squeezes while BTC flat
BTC hedge loses while shorts also lose
```

Add rules:

```text
if short gross exposure stale:
    freeze hedge changes or flatten hedge

if short gross exposure near zero:
    flatten hedge

if hedge age > max:
    alert/block

if hedge PnL and short PnL both negative beyond threshold:
    reduce gross exposure
```

---

# 17. Phase 12 — Portfolio and Sleeve Safety

The architecture must prevent one sleeve from accidentally changing another sleeve’s risk.

## 17.1 Symbol authority

For each symbol, centralize:

```text
exchange net position
ledger position by sleeve
open entry orders
open reduce-only orders
open TP/SL orders
pending orders
venue stop status
unprotected quantity
allowed new quantity
```

No sleeve should trade a symbol if the central allocator says the exchange/ledger state is dirty.

## 17.2 Risk health gate

Add a global gate:

```text
if risk service unhealthy:
    no new entries

if WS stale:
    no new entries

if exchange position != ledger beyond tolerance:
    no new entries

if any non-hedge position unprotected beyond age threshold:
    no new entries

if stop repair failing:
    no new entries

if private execution stream down:
    no new entries
```

## 17.3 Trade lifecycle state machine

Every trade should have explicit states:

```text
SIGNAL_CREATED
ENTRY_APPROVED
ORDER_PREPARED
ORDER_SUBMITTED
PARTIAL_FILL
FILLED
PROTECTION_PENDING
PROTECTED
EXIT_SIGNALLED
EXIT_ORDER_SUBMITTED
EXIT_PARTIAL
CLOSED
RECONCILED
FAILED
ORPHAN
ADOPTED
FORCE_FLATTENED
```

Block unsafe transitions.

Example:

```text
FILLED -> PROTECTED must happen within N seconds.
Otherwise flatten or block all new entries.
```

---

# 18. Phase 13 — Live Shadow Validation

After research changes, do not jump directly to full live.

Run a shadow/live-paper comparison.

## 18.1 Shadow mode

For each signal, simulate:

```text
paper immediate entry
paper delayed entries
paper adverse-limit entries
paper conditional scale-in
paper catastrophic stop
paper signal-invalidation stop
paper skip model
```

Compare against actual demo fills.

## 18.2 Live-demo metrics

Track:

```text
signal latency
order latency
fill latency
fill rate
PostOnly cancel rate
REST fallback rate
WS fill confirmation time
ledger mismatch count
stop placement latency
stop repair count
unprotected position seconds
slippage
fees
funding
maker/taker split
```

Research is not complete until live-demo path behavior matches backtest assumptions.

2026-06-28 forward-readiness note:
`continuous-forward-readiness` now has a current v2 report at
`reports/continuous_forward_readiness/2026-06-28-v2-current/`. The gate was run
from baseline clock `2026-06-18T19:54:00Z` with
`strategy_profile=continuous_ensemble_v2`, paper strategy
`continuous_fade_v2_paper`, and demo strategy `continuous_fade_v2`. Paper and
demo rebalance-cycle audits passed (121 paper cycles / 123 demo cycles, no
rebalance telemetry issues). Paper and demo operational-cycle audits also passed
(0 entry-risk blocks, 0 order failures, 0 unprotected-position seconds), but
there were 0 paper trades, 0 demo trades, and 0 paired trades. Forward readiness
is therefore blocked by sample size, not by observed paper/demo drift or
cycle-level operational anomalies. No slippage, fill-rate, fee,
funding-performance, maker/taker, stop-placement-latency, or stop-repair claim
can be made yet.

---

# 19. Decision Framework

Use predefined promotion gates.

## 19.1 Promotion gate

A candidate variant must beat baseline on at least one of:

```text
lower expected shortfall
lower worst-trade loss
lower drawdown
higher PnL per signal
higher robustness
lower operational risk
```

It does not need to beat baseline Sharpe if it materially reduces tail risk.

## 19.2 Required robustness

For a candidate to replace baseline:

```text
works across BTC lookbacks
works across nearby TP values
works across nearby max-hold values
works across liquidity thresholds
works year-by-year
works by component
does not rely on one symbol
does not rely on one month
does not rely on one market regime
survives worse execution
survives synthetic squeeze injection
```

## 19.3 Rejection rules

Reject a change if:

```text
it improves Sharpe but worsens worst-trade risk materially
it improves MAR only by eliminating many trades without stable rationale
it works only at one parameter value
it depends on perfect maker fills
it worsens PnL per original signal
it increases full-size exposure during failed fades
it increases unprotected position time
it increases sleeve/account reconciliation risk
```

---

# 20. Concrete Experiment Backlog

## Batch A — Immediate diagnostics

```text
A1. MAE/MFE table
A2. Winner vs loser MAE distribution
A3. Event-time path curves
A4. PnL by BTC regime
A5. PnL by year
A6. PnL by component
A7. PnL by liquidity bucket
A8. Worst 10 trades manual review
A9. Active-period Sharpe
A10. Trade-cluster-adjusted Sharpe
```

Expected result:

```text
You will discover whether the edge is broad or concentrated.
```

## Batch B — Timing

```text
B1. Delay grid
B2. Adverse-limit grid
B3. Failed-high entry
B4. Volume-deceleration entry
B5. OI-deceleration entry
B6. Conditional scale-in variants
B7. TWAP clip attribution
B8. PnL per original signal comparison
```

Expected result:

```text
You will know whether immediate entry is genuinely best or just best on average.
```

## Batch C — Stops

```text
C1. Fixed stop frontier
C2. Volatility stop frontier
C3. ATR stop frontier
C4. Catastrophic stop only
C5. Time-underwater stop
C6. Signal-invalidation stop
C7. Portfolio drawdown kill-switch
C8. Venue disaster stop simulation
```

Expected result:

```text
You will know whether stops are bad in general or only bad when placed inside normal adverse excursion.
```

## Batch D — Regime

```text
D1. BTC lookback sweep
D2. BTC regime delay
D3. BTC regime perturbation
D4. Binary vs scaling regime
D5. BTC + volatility regime
D6. BTC + ETH confirmation
D7. Chop filter
```

Expected result:

```text
You will know whether the BTC filter is robust or too magical.
```

## Batch E — Tail

```text
E1. Worst-trade doubling
E2. Synthetic +50% squeeze
E3. Synthetic +100% squeeze
E4. Multi-symbol correlated squeeze
E5. Exchange outage during squeeze
E6. Risk daemon failure during squeeze
E7. Margin/liquidation distance simulation
E8. Cluster bootstrap risk-of-ruin
```

Expected result:

```text
You will know whether tiny sizing is actually enough.
```

## Batch F — Execution

```text
F1. All-taker cost model
F2. Maker fill probability model
F3. PostOnly cancel model
F4. Passive fill adverse-selection study
F5. Stop-market slippage model
F6. Partial-fill simulation
F7. REST/WS failure simulation
F8. Reconciliation state-machine test
```

Expected result:

```text
You will know whether backtest fills survive live mechanics.
```

---

# 21. Recommended Target Design After Research

Based on the evidence so far, provisional target design:

```text
Entry:
    50–80% immediate entry

Add:
    optional conditional add only at better price
    only if signal remains valid
    only if adverse move shows exhaustion, not continuation

No TWAP:
    remove naive clock-based slicing

Normal stop:
    none, or very wide only

Disaster stop:
    mandatory exchange-side or reduce-only emergency protection

Exit:
    existing TP/max-hold retained initially
    add signal-invalidation exit
    add time-underwater exit if validated

Sizing:
    inverse-vol + BTC-risk sizing retained
    add loss-at-disaster cap
    add portfolio heat cap

Regime:
    BTC filter retained
    test binary vs scaling
    profile must pin filter config

Portfolio:
    circuit breakers mandatory
    no new entries if risk/reconciliation unhealthy

Hedge:
    threshold-based rebalance added
    hedge PnL separated from alpha PnL
```

This keeps the structure that seems to work while addressing the actual weaknesses.

---

# 22. Most Important Tables to Produce

## Table 1 — MAE conditional recovery

| MAE threshold reached | Trades | % of trades | Eventually profitable | Avg final PnL | Median final PnL | 5% tail | Avg recovery time |
|---:|---:|---:|---:|---:|---:|---:|---:|
| >2% | | | | | | | |
| >5% | | | | | | | |
| >10% | | | | | | | |
| >20% | | | | | | | |
| >40% | | | | | | | |

This tells you where stops belong.

## Table 2 — Timing by original signal

| Entry method | Fill rate | PnL/signal | PnL/fill | Sharpe | MAR | Worst trade | Avg MAE | Missed winners |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Immediate | | | | | | | | |
| 30m delay | | | | | | | | |
| 1h delay | | | | | | | | |
| +1% adverse | | | | | | | | |
| +1 ATR adverse | | | | | | | | |
| Failed high | | | | | | | | |
| Conditional scale-in | | | | | | | | |

This tells you whether timing is improvable.

## Table 3 — Stop frontier

| Stop | Stop hits | Post-stop TP hit rate | Net PnL | MAR | Worst trade | ES 95 | Comment |
|---|---:|---:|---:|---:|---:|---:|---|
| 5% | | | | | | | |
| 10% | | | | | | | |
| 20% | | | | | | | |
| 40% | | | | | | | |
| Disaster-only | | | | | | | |
| None | | | | | | | |

This tells you whether stops are bad or just badly placed.

## Table 4 — BTC regime robustness

| BTC filter | Trades | Sharpe | MAR | Max DD | Worst month | Worst trade | Comment |
|---|---:|---:|---:|---:|---:|---:|---|
| Off | | | | | | | |
| 20d | | | | | | | |
| 30d | | | | | | | |
| 40d | | | | | | | |
| 60d | | | | | | | |
| Scaled | | | | | | | |
| Delayed 24h | | | | | | | |

This tells you whether the regime filter is real.

## Table 5 — Synthetic squeeze survival

| Scenario | Equity DD | Margin peak | Liquidation distance | Time to recover | Strategy survives? |
|---|---:|---:|---:|---:|---|
| One coin +50% | | | | | |
| One coin +100% | | | | | |
| Three coins +50% | | | | | |
| BTC +10%, alts +30% | | | | | |
| Exchange down 1h during squeeze | | | | | |

This tells you whether small sizing is small enough.

---

# 23. Ranking of Likely Improvements

| Rank | Improvement | Expected value | Risk of overfit |
|---:|---|---:|---:|
| 1 | MAE/MFE diagnostics | Very high | Low |
| 2 | Skip worst 5–15% of trades | Very high | Medium |
| 3 | Catastrophic stop + sizing cap | High | Low |
| 4 | Portfolio drawdown/heat kill-switch | High | Low |
| 5 | Signal-invalidation exit | High | Medium |
| 6 | Conditional scale-in | Medium/high | Medium |
| 7 | BTC regime robustness/scaling | Medium/high | Medium |
| 8 | Execution realism improvements | Medium/high | Low |
| 9 | Time-underwater exit | Medium | Medium |
| 10 | Better hedge rebalance | Medium | Medium |
| 11 | Fixed/tight stop | Low / likely bad | Low |
| 12 | TWAP | Low / likely bad | Low |
| 13 | Complex ML classifier | Unknown | High |

Likely best research direction:

```text
skip logic + disaster containment
```

not:

```text
better average entry through TWAP or tight stops
```

---

# 24. What Would Change the Assessment

## More bullish if:

```text
MAE/MFE shows clear recoverable adverse behavior
20d–60d BTC filters all work
performance is stable by year
no single component dominates
worst 10 trades do not define the system
synthetic +100% squeeze is survivable
all-taker execution remains profitable
cluster-adjusted Sharpe remains strong
catastrophic stop barely affects expectancy
skip logic reduces tails without killing PnL
```

## More bearish if:

```text
30d BTC filter is uniquely magical
PnL comes from one year or one regime
winners and losers are indistinguishable until too late
worst 5 trades dominate drawdown
synthetic squeeze destroys MAR
all-taker model kills edge
delayed regime signal kills edge
backtest assumes unrealistic passive fills
skip logic cannot identify any bad trades
DSR/PBO results are poor
```

---

# 25. Final Deployment Standard

Before increasing size, require:

```text
1. Frozen baseline report
2. MAE/MFE report
3. Timing report using PnL per original signal
4. Stop frontier report
5. BTC regime robustness report
6. Component ablation report
7. Cost/slippage/funding report
8. Synthetic squeeze report
9. Cluster-adjusted performance report
10. Live-demo execution report
11. Risk daemon failure-mode report
12. Disaster-stop implementation test
13. Portfolio kill-switch test
14. Profile hash/reproducibility implementation
```

Minimum acceptable live design:

```text
No tight tactical stop required,
but every position has disaster loss accounting.

No naive TWAP,
but conditional adds allowed only when signal remains valid.

BTC filter retained,
but it must be robust across nearby definitions.

Small sizing retained,
but capped by loss-at-disaster.

Risk process retained,
but new entries blocked if risk/reconciliation is unhealthy.

Backtest Sharpe not trusted
unless it survives costs, clusters, DSR/PBO, and synthetic squeezes.
```

---

# 26. Implementation Checklist

## Immediate code/data tasks

```text
[x] Add signal table with skipped/unfilled signals.
[x] Add trade path table with forward returns and max adverse/favorable moves.
[x] Add profile hash to every signal and trade row.
[x] Add git commit to every signal and trade row.
[x] Add BTC regime value and config to every signal row.
[x] Add component ID and component score to every signal row.
[x] Add funding, OI, spread, and depth snapshots where available.
[x] Add MAE/MFE computation to research pipeline.
[x] Add PnL per original signal metric.
[x] Add active-period and cluster-adjusted Sharpe metric.
[x] Add synthetic squeeze injector.
[x] Add cost/slippage scenario runner.
[x] Add BTC regime robustness runner.
[x] Add stop frontier runner.
[x] Add timing-grid runner.
[x] Add conditional scale-in simulator.
[x] Add signal-invalidation simulator.
[x] Add conditional scale-in full component+hedge replay.
[x] Add portfolio heat calculator.
[x] Add disaster-loss sizing calculator.
```

## Immediate research outputs

```text
[x] Baseline report.
[x] MAE/MFE report.
[x] Winner-vs-loser path report.
[x] Timing report.
[x] Stop frontier report.
[x] BTC regime robustness report.
[x] Component ablation report.
[x] Synthetic squeeze report.
[x] Cost/slippage/funding report.
[x] Worst-trade dependency report.
```

2026-06-28 artifact audit: the frozen baseline run
`research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/` now
backs the checked research-tooling/report items above. Evidence includes
`run_metadata.json` (`run_label=exploratory`, full-PIT roots, explicit
decision/data/order/fill timestamps), `tables/artifact_index.json`, and
`reports/final_research_report.md`. Current table coverage is 25,845
`signal_table.parquet` rows (4,519 selected / 21,326 skipped or unfilled),
25,845 `forward_path_by_signal.parquet` rows, 4,519 `trades_enriched.parquet`
rows, and 4,519 `trade_path_metrics.parquet` rows. Signal rows carry
`profile_hash`, `git_commit`, BTC trend/regime fields, component ids, component
scores, and timing provenance. Trade rows carry `profile_hash`, `git_commit`,
component ids/weights, funding fields, MAE/MFE, and path timing fields. OI,
spread, and depth snapshots were unavailable in this frozen tape and are not
claimed as active predictors. The result remains `exploratory`, not
`candidate` or `paper_ready`; forward demo/paper remains the OOS arbiter.

2026-06-28 scale-in diagnostic note:
`scripts/continuous_scale_in_diagnostic.py` now writes
`conditional_scale_in_by_trade.csv` and `conditional_scale_in_summary.csv` over
the frozen baseline tape and refreshes the final report. The best diagnostic arm
was 5% MAE trigger / 50% add-on: Bybit component net changed from 20.89% to
29.85% with 54.63% fill rate, and Binance changed from 14.69% to 20.84% with
53.21% fill rate. This is path-conditioned, leverage-like, and exploratory; it
is not a full component+hedge portfolio replay and is not live-sizing evidence.

2026-06-28 scale-in portfolio replay note:
`scripts/continuous_scale_in_portfolio_replay.py` now writes
`scale_in_portfolio_replay.csv` and component overlay artifacts under
`portfolio_replays/scale_in_grid/`. The replay creates explicit child shorts,
recomputes component MTM, recombines the deployed ensemble, and reapplies the
BTC/ETH hedge. All three preregistered arms lifted return, but every arm
worsened MAR and drawdown on both venues. Best MAR arms were Bybit
`mae05_add25` (+31.17%/MAR 6.75/DD -1.43% vs baseline +26.64%/7.33/-1.13%) and
Binance `mae10_add50` (+23.54%/MAR 5.36/DD -1.36% vs +18.84%/5.72/-1.02%).
Reject live/paper scale-in behavior from this evidence.

2026-06-28 signal-invalidation diagnostic note:
`scripts/continuous_signal_invalidation_diagnostic.py` now writes
`signal_invalidation_by_trade.csv`, `signal_invalidation_summary.csv`,
`signal_invalidation_hourly_state_panel.parquet`, and
`signal_invalidation_state_panel_summary.csv` over the frozen baseline
signal/trade tape and refreshes the final report. The simulator uses only
explicit future same-symbol candidate rows; absence of a row is not treated as
invalidation because `signal_table.parquet` is sparse. Active
candidate-pressure arms reduced component net on both venues. The least harmful
active arm was `candidate_pressure_3h_score99`: Bybit changed from 20.89% to
17.85% with 8.91% invalidation rate, and Binance changed from 14.69% to 12.87%
with 5.90% invalidation rate. The BTC-trend rejection arm had zero in-window
hits. The hourly coverage audit found Bybit candidate-state/OI/funding/BTC
coverage of 2.45%/67.55%/100.00%/100.00% over 48,447 state rows and Binance
2.25%/7.12%/99.74%/100.00% over 44,416 rows; spread/depth and sector proxy
coverage remain 0.00%, so the full state panel is not ready. This closes the
simulator/tooling item as exploratory negative/missing-data evidence; do not
add a live invalidation exit without a full hourly state panel and a full
component+hedge replay.

2026-06-28 DSR/PBO diagnostic note:
`scripts/continuous_overfit_diagnostic.py` now writes
`overfit_variant_universe.csv`, `deflated_sharpe.csv`,
`pbo_cscv_summary.csv`, and `pbo_cscv_splits.csv` from existing frozen
full-portfolio replay artifacts only. It does not run new strategy variants.
Across 21 full-replay variants per venue, PBO was 41.43% Bybit / 35.71%
Binance and baseline DSR probability was 23.17% / 20.08%. Best-Sharpe variants
were `mae05_add25` on Bybit and `skip_btc_tail_035` on Binance, both already
rejected by their preregistered deployment rules. Treat the replay surface as
inference-fragile; internal Sharpe/MAR rankings are not deployment proof.

## Immediate live-safety tasks

```text
[x] Add global risk-health gate for new entries.
[x] Block new entries if WS/private execution stream is stale.
[x] Block new entries if exchange position and ledger disagree for continuous-attributable positions.
[x] Block new entries if non-hedge position lacks disaster protection.
[x] Add unprotected-position timer.
[x] Add trade lifecycle state machine.
[x] Add append-only risk event log.
[x] Add stop placement/repair audit logs.
[x] Add portfolio heat cap.
[x] Add account-level drawdown kill-switch.
```

2026-06-28 progress note: `continuous_demo.py` now has a submit-mode
entry-risk-health gate that records `entry_risk_health_*` cycle fields and
alerts when blocked. It covers private snapshot errors, stale private execution
WS after the stream has emitted, and continuous-ledger-open symbols missing from
the venue position snapshot.

2026-06-28 progress note: the same gate now also blocks live exchange-only
positions when the continuous order ledger has a recent non-reduce-only entry
attempt for the symbol but no open continuous trade row. Raw exchange-only
positions with no continuous order evidence remain a ws_risk/reconciliation
authority task. Blocked submit cycles also append `continuous_risk_events.jsonl`
rows.

2026-06-28 progress note: the gate now blocks new submitted entries while an
open non-hedge continuous position has no venue `stopLoss` in the private
position snapshot. The current v2 profile still has `STOP_LOSS_PCT=0`; this is
a safety brake on adding exposure while primary positions are unprotected, not a
claim that the no-stop policy is acceptable.

2026-06-28 progress note: unprotected-position timer telemetry is now recorded
as `entry_risk_health_unprotected_position_ages` and
`entry_risk_health_unprotected_max_age_seconds` on cycle rows and blocked risk
events.

2026-06-28 progress note: initial continuous lifecycle-state telemetry is now
recorded as `entry_risk_health_lifecycle_states` plus focused counts for
`PROTECTION_PENDING` and `ORPHAN`. This derives explicit states from the ledger
and private position snapshot, but does not yet enforce a full transition table;
the lifecycle state-machine checklist item stays open.

2026-06-28 progress note: submitted cycles now enforce terminal lifecycle
transitions before flushing trade rows. A closed trade cannot be reopened, and a
close row without a prior trade is rejected and appended to
`continuous_risk_events.jsonl` as `lifecycle_transition_rejected`. This is a
real guard against ledger corruption, but not the complete transition table, so
the lifecycle state-machine checklist item remains open.

2026-06-28 progress note: the submitted-row lifecycle guard now has an explicit
legal transition table. It rejects protected-row regressions back to
`PROTECTION_PENDING` unless the protected state was intentionally preserved on a
plain open-row update, and it rejects loss of an in-flight exit marker. This is
still not a complete lifecycle state machine at this checkpoint because
venue-snapshot `PROTECTED` promotion is not yet persisted into trade-row state.

2026-06-28 progress note: submitted live cycles with a healthy private position
snapshot now persist monotonic `PROTECTED` promotions onto full copied trade rows
before ledger flush, with `lifecycle_state_source=private_position_snapshot` and
`lifecycle_state_updated_at_ms`. Missing stops are not demoted into the ledger;
they remain entry-risk-health blocks. At this checkpoint the lifecycle checklist
item still stayed open because preflight/order-prepared lifecycle states were
split across order rows rather than a single end-to-end trade lifecycle event
stream.

2026-06-28 progress note: submitted live cycles now append
`continuous_lifecycle_events.jsonl` lifecycle transition rows for crash-safe
preflight/order-prepared events, final order events, and accepted trade-row
state writes. The event stream covers `ORDER_PREPARED`, `ORDER_SUBMITTED`,
`PARTIAL_FILL`, `FILLED`, `PROTECTED`, `EXIT_ORDER_SUBMITTED`, `EXIT_PARTIAL`,
`CLOSED`, and `FAILED` transitions with deterministic `event_key` fields for
downstream de-duplication. This closes the immediate lifecycle state-machine
checklist item. `RECONCILED` and `FORCE_FLATTENED` remain reserved states, not
active flow claims.

2026-06-28 progress note: `ws_risk` now appends stop/take-profit repair attempts
to `reports/event-risk-ws/stop_audit_events.jsonl` after sleeve tagging, with
target/current protection prices, submit status, routed sleeve, link, and error
text. Entry-side stop placement is still represented in the order ledger and the
continuous entry-health/unprotected-position telemetry.

2026-06-28 progress note: submit-mode entries now apply a portfolio heat cap
before candidate selection. The default is 5% of equity under a +100% adverse
shock, computed from current non-hedge open notional plus conservative per-entry
heat; dry-run/paper cycles record the telemetry but are not clamped.

2026-06-28 progress note: submit-mode entries now block when current wallet
equity is more than 2% below the prior healthy cycle high-water mark. Snapshot
errors do not trip this rule on fallback equity; they already block through the
private-snapshot health gate. Cycle rows record `entry_account_drawdown_*`. The
forward-readiness operational audit now fails explicitly on account-drawdown
kill-switch rows, even when mixed-version rows lack newer
`entry_risk_health_reasons` text; the top-level readiness report also surfaces
portfolio-heat clamp and account-drawdown activation counts.

---

# 27. Final View

Do not force a normal stop. The evidence suggests tight stops are structurally wrong.

Do not force TWAP. The evidence suggests time-slicing damages the edge.

The correct research path is:

```text
1. Map the path.
2. Identify normal adverse excursion.
3. Identify failed-fade continuation.
4. Keep immediate exposure where alpha decays fast.
5. Add only conditionally, not mechanically.
6. Skip the worst setups.
7. Use catastrophic risk containment.
8. Size by disaster loss, not average volatility alone.
9. Validate the BTC filter hard.
10. Stress the system until it breaks.
```

The strategy may be good. The job now is to find out whether it is good because it has real mean-reversion alpha, or because it has not yet met the tail event it is implicitly short.
