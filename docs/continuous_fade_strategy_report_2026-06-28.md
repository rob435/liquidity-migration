# Continuous Fade Strategy Report

Date: 2026-06-28

Strategy: `continuous_ensemble_v2`

Run label: exploratory

Real money status: no. Demo/paper only. Mainnet is out of scope without explicit
owner instruction and fresh forward evidence.

Primary artifact root:
`research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/`

## Executive View

`continuous_ensemble_v2` is a short-horizon crypto perpetual short-fade book.
It shorts newly crowded, high-turnover, high-pop symbols inside a weak
residual-momentum cross-section, holds for a maximum of 24 hours, and takes
profit after a 12% favorable move. The current local target is an ensemble of
three closely related component signals, inverse-vol sized, BTC/ETH hedged, and
protected by live operational risk gates rather than tight tactical stops.

The internal evidence supports a real, repeatable mechanism: fast and slow
mean-reversion after crowded hourly pops. The evidence also shows the strategy
is short adverse-tail risk. Large MAE states are dangerous, and naive attempts
to "fix" them with delay, TWAP-like waiting, adverse-limit entries, or fixed
price stops generally reduce portfolio quality.

The current conclusion is conservative:

- Keep the near-immediate entry lifecycle.
- Keep the 30-day BTC uptrend gate.
- Keep the `CTRL_BTC_RISK_70_90_35` sizing overlay.
- Keep the 12% TP / 24h max-hold lifecycle.
- Do not add fixed tactical stops, naive delay, adverse-limit entry, scale-ins,
  or signal-invalidation exits from current evidence.
- Keep size small and enforce disaster-loss, portfolio-heat, reconciliation,
  and account-drawdown controls.
- Treat all internal replay statistics as exploratory until forward demo/paper
  has a real trade sample.

The offline research checklist is mostly complete. The strategy is not
paper-ready in the evidence sense because forward execution has 0 demo trades,
0 paper trades, and 0 paired paper/demo trades after the current v2 clock.

## Evidence Status

| Item | Status |
| --- | --- |
| Full-PIT Bybit/Binance research roots | Present and used |
| Baseline run identity | Frozen under `continuous_ensemble_v2_baseline_current` |
| Profile hash | `c4eb2eed1658697aa1239afd847e0de9d04f87ffe98080d4607ea6c1fd86a4f6` |
| Code commit in run metadata | `9644fec16427cec12296f906200918c00c7c8b8a` |
| Run label | `exploratory` |
| Forward demo/paper trade sample | Missing |
| Mainnet approval | None |

The correct evidence label is `exploratory`, not `candidate` or `paper_ready`.
The internal window is spent by repeated diagnostics. Forward demo/paper is the
only clean OOS arbiter left.

## Strategy Specification

### Universe And Data

Research uses the per-venue full-PIT roots:

| Venue | Root | Research window |
| --- | --- | --- |
| Bybit | `~/SHARED_DATA/bybit_full_pit` | 2023-04-05 to 2026-06-25 |
| Binance | `~/SHARED_DATA/binance_full_pit` | 2023-04-01 to 2026-06-24 |

The roots are perpetuals-only, manifest-gated, and include delisted or migrated
symbols where available. Research uses 1h klines, 5m paths for path diagnostics,
funding, residual momentum, and PIT archive membership.

Known data limitations:

- Binance funding mode in the baseline summary is `partial`.
- OI coverage in the hourly state panel is weak for Binance.
- Spread/depth and sector-proxy coverage are 0% in the current hourly
  invalidation state panel.
- Forward execution evidence is absent.

### Signal Model

The alpha hypothesis is an intraday crowding/fade pattern:

1. Build causal trailing features from closed hourly bars.
2. Join daily residual momentum by day floor with causal availability.
3. Filter to low residual momentum symbols: `rmom_quantile=0.25`.
4. Rank `max_ret168`, the maximum single-hour return over the trailing 168h.
5. Select the top composite decile after liquidity and event triggers.
6. Enter short after the configured one-bar confirmation delay.

Core feature and gate settings:

| Field | Current value |
| --- | --- |
| Side | Short |
| Residual momentum quantile | Lowest 25% |
| Feature set | `max_ret168` |
| Liquidity floor | Hourly turnover quote >= 500,000 |
| Listing age floor | 240 days |
| BTC regime gate | Prior 30d BTC uptrend |
| Max active shorts | 25 |
| Entry crowding cap | Fresh candidate count <= 2 |
| Entry delay | 1 hour / one closed bar confirmation |
| Hold | 24h max |
| Take profit | 12% favorable move |
| Tactical stop | Disabled |
| Funding | Enabled where root coverage exists |

The live target described in `docs/promoted_trading_logic.md` adds max-new-entry
cycle controls, sniper execution watch, paper/demo state, and runtime risk
gates. The frozen research replay is therefore the official local research
target, not a guarantee that every live daemon path has executed.

### Components

The ensemble has three components:

| Tag | Component cell | Trigger | Weight |
| --- | --- | --- | ---: |
| `turn3p3` | `merged_signal` | turnover spike >= 3x and 1h pop >= 3% | 0.333333 |
| `turn4p3` | `age240_turn4pop3_crowd2` | turnover spike >= 4x and 1h pop >= 3% | 0.222222 |
| `turn4p5` | `age240_turn4pop5_crowd2` | turnover spike >= 4x and 1h pop >= 5% | 0.444444 |

Each component uses gross exposure `0.5`, max active 25, inverse-vol sizing,
12% TP, 24h max hold, and no tactical stop.

### Sizing

Base component sizing:

```text
base_notional = gross_exposure / max_active
inverse_vol_multiplier = target_vol_per_name / rv_168h
inverse_vol_multiplier is clamped to [0.5, 2.0]
```

Live order notional additionally applies component weight, wallet balance
fraction, rebalance scale, and BTC-risk stack multiplier.

Current important sizing controls:

| Control | Current read |
| --- | --- |
| Inverse-vol target | `TARGET_VOL_PER_NAME=0.01` |
| Vol clamp | `[0.5, 2.0]` |
| BTC-risk overlay | `CTRL_BTC_RISK_70_90_35` |
| BTC-risk tail multiplier | `0.35` |
| Portfolio heat cap | default 5% equity loss under +100% adverse shock |
| Account drawdown kill-switch | block if wallet equity >2% below healthy high-water mark |

The BTC-risk overlay improved MAR/drawdown on both venues in prior local
research but cut Binance return. It is a narrow demo/paper sizing experiment,
not broad acceptance proof.

### Hedge

The portfolio includes a BTC+ETH factor hedge:

| Hedge field | Value |
| --- | --- |
| Instruments | BTCUSDT and ETHUSDT |
| Beta window | 90 days |
| Min observations | 60 |
| Hedge cap | 2.0 |
| Hedge cost | 5 bps |
| Regime overlay | BTC-vol regime, `lam=0.5`, `vol_window=30`, `pct_window=250` |

Baseline hedge attribution:

| Venue | Short gross return sum | Entry cost | Funding | Hedge return | Hedge funding | Hedge cost | Basket return sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | 26.03% | -2.04% | -2.78% | +2.83% | -0.22% | -0.13% | 23.70% |
| Binance | 18.48% | -1.73% | -1.90% | +2.83% | -0.20% | -0.15% | 17.33% |

The hedge contributes meaningfully in the frozen sample. It is not a
liquidation engine and does not solve single-name squeeze risk.

## Baseline Performance

Full portfolio baseline:

| Venue | Window | Component trades | Total return | Annualized | Max DD | MAR | Sharpe ann | Worst day |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | 2023-04-05 to 2026-06-25 | 2,367 | +26.64% | +7.60% | -1.13% | 7.33 | 3.37 | -0.93% |
| Binance | 2023-04-01 to 2026-06-24 | 2,152 | +18.84% | +5.48% | -1.02% | 5.72 | 2.63 | -0.63% |

These are attractive internal statistics, but they are not accepted alpha
evidence because the forward sample is absent and the replay surface is
inference-fragile.

### Year Contribution

Component-ledger net PnL by year:

| Venue | Year | Trades | Net PnL | Win rate | Worst trade |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | 2023 | 441 | +2.76% | 66.44% | -0.383% |
| Bybit | 2024 | 718 | +4.22% | 65.32% | -0.332% |
| Bybit | 2025 | 758 | +7.87% | 70.32% | -0.305% |
| Bybit | 2026 | 450 | +6.04% | 73.33% | -0.291% |
| Binance | 2023 | 375 | +2.56% | 62.67% | -0.153% |
| Binance | 2024 | 624 | +2.96% | 61.70% | -0.191% |
| Binance | 2025 | 478 | +3.71% | 66.11% | -0.290% |
| Binance | 2026 | 675 | +5.46% | 68.00% | -0.334% |

The result is not carried by one calendar year. That is positive. It is still
not clean OOS.

### Component Contribution

Component-ledger summary:

| Venue | Component | Trades | Net PnL | Win rate | Avg MAE | Worst trade |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | `turn3p3` | 857 | +7.17% | 67.79% | 10.16% | -0.287% |
| Bybit | `turn4p3` | 800 | +4.67% | 68.25% | 10.11% | -0.191% |
| Bybit | `turn4p5` | 710 | +9.05% | 70.14% | 10.35% | -0.383% |
| Binance | `turn3p3` | 799 | +5.30% | 64.33% | 10.23% | -0.275% |
| Binance | `turn4p3` | 730 | +3.25% | 63.97% | 10.46% | -0.167% |
| Binance | `turn4p5` | 623 | +6.15% | 66.45% | 11.17% | -0.334% |

No single component is the entire book. Equal-weight component recombination is
close to current-weight recombination, which weakens the argument that the edge
is purely weight-fitting.

## Path Anatomy

The strategy makes money when crowded pops mean-revert quickly enough. It loses
when the pop continues and becomes a failed fade.

Path label contribution:

| Venue | Path label | Trades | Net PnL | Avg PnL | Avg MAE | Worst trade |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | FAST_REVERT | 1,027 | +31.34% | +0.0305% | 3.82% | ~0.000% |
| Bybit | SLOW_REVERT | 415 | +10.70% | +0.0258% | 5.03% | ~0.000% |
| Bybit | SQUEEZE_REVERT | 183 | +4.37% | +0.0239% | 18.09% | ~0.000% |
| Bybit | FAILED_FADE | 659 | -23.17% | -0.0352% | 19.80% | -0.332% |
| Bybit | DISASTER | 13 | -1.76% | -0.1352% | 118.58% | -0.383% |
| Binance | FAST_REVERT | 868 | +27.43% | +0.0316% | 3.41% | ~0.000% |
| Binance | SLOW_REVERT | 386 | +9.62% | +0.0249% | 4.88% | ~0.000% |
| Binance | SQUEEZE_REVERT | 138 | +3.65% | +0.0264% | 17.60% | ~0.000% |
| Binance | FAILED_FADE | 644 | -22.71% | -0.0353% | 19.08% | -0.287% |
| Binance | DISASTER | 19 | -2.43% | -0.1280% | 151.10% | -0.334% |

Interpretation:

- `FAST_REVERT` and `SLOW_REVERT` are the core alpha source.
- `SQUEEZE_REVERT` is uncomfortable but still profitable in aggregate.
- `FAILED_FADE` is the primary leakage path.
- `DISASTER` rows are rare but define the sizing problem.

## MAE And Adverse Move Risk

The most important risk finding is not the headline drawdown. It is the MAE
conditional distribution.

Recovery after MAE thresholds:

| Venue | MAE threshold | Trades | Share of trades | Eventually profitable | Avg final PnL | 5% tail |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | 5% | 1,293 | 54.63% | 49.11% | -0.0071% | -0.0865% |
| Bybit | 10% | 772 | 32.62% | 36.27% | -0.0204% | -0.1101% |
| Bybit | 20% | 334 | 14.11% | 19.76% | -0.0448% | -0.1880% |
| Bybit | 40% | 73 | 3.08% | 9.59% | -0.1090% | -0.2919% |
| Binance | 5% | 1,145 | 53.21% | 43.76% | -0.0107% | -0.0980% |
| Binance | 10% | 689 | 32.02% | 30.48% | -0.0251% | -0.1364% |
| Binance | 20% | 278 | 12.92% | 15.47% | -0.0537% | -0.1960% |
| Binance | 40% | 74 | 3.44% | 8.11% | -0.1097% | -0.2779% |

Conclusion:

- 5% to 10% MAE is normal for this strategy.
- 10% to 20% MAE is where expectancy deteriorates.
- 20%+ MAE is usually a failed fade.
- 40%+ MAE is tail territory and should be treated as risk-budget evidence,
  not as a normal recoverable state.

This does not imply a 20% stop. The full replay rejected fixed stops. It implies
position sizing, heat caps, disaster-loss budgets, and circuit breakers matter
more than tight tactical stops.

## Timing Research

Timing verdict: do not wait.

Full component + BTC-risk + hedge replay:

| Venue | Arm | Trades | Return | MAR | Max DD | Sharpe | Worst day |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | Baseline | 2,367 | +26.64% | 7.33 | -1.13% | 3.37 | -0.93% |
| Bybit | +1h delay | 2,367 | +23.58% | 5.23 | -1.40% | 3.09 | -0.77% |
| Bybit | +2h delay | 2,367 | +20.54% | 6.35 | -1.00% | 2.86 | -0.78% |
| Bybit | +1% adverse-limit | 2,131 | +24.39% | 5.94 | -1.27% | 3.21 | -0.90% |
| Binance | Baseline | 2,152 | +18.84% | 5.72 | -1.02% | 2.63 | -0.63% |
| Binance | +1h delay | 2,152 | +17.41% | 4.75 | -1.13% | 2.62 | -0.93% |
| Binance | +2h delay | 2,155 | +16.80% | 5.32 | -0.98% | 2.70 | -0.77% |
| Binance | +1% adverse-limit | 1,900 | +16.59% | 3.75 | -1.37% | 2.43 | -0.54% |

The 5m path diagnostics agreed: 15m delay, 30m delay, and next-red 15m all
underperformed immediate or near-immediate entry on both venues.

Interpretation: the alpha is front-loaded. Waiting improves some entry prices
but loses enough fast reversals that portfolio quality deteriorates.

## Stop Research

Fixed price stops were rejected.

| Venue | Arm | Return | MAR | Max DD | Sharpe |
| --- | --- | ---: | ---: | ---: | ---: |
| Bybit | Baseline | +26.64% | 7.33 | -1.13% | 3.37 |
| Bybit | 20% fixed stop | +9.50% | 0.94 | -3.15% | 1.25 |
| Bybit | 40% fixed stop | +21.03% | 3.38 | -1.93% | 2.65 |
| Bybit | 80% fixed stop | +23.96% | 4.68 | -1.59% | 2.89 |
| Binance | Baseline | +18.84% | 5.72 | -1.02% | 2.63 |
| Binance | 20% fixed stop | +7.55% | 1.48 | -1.58% | 1.09 |
| Binance | 40% fixed stop | +12.70% | 2.16 | -1.82% | 1.66 |
| Binance | 80% fixed stop | +13.97% | 2.50 | -1.73% | 1.87 |

Interpretation: the book needs room for normal adverse excursion. Simple price
stops cut winners and crystallize noisy squeezes. The correct control is not a
tight stop. It is smaller notional, heat accounting, disaster-loss budgets,
operational health gates, and account-level drawdown brakes.

## BTC Regime Research

The current 30d BTC uptrend gate should remain. Gate-off and lookback retunes
were rejected.

| Venue | Arm | Trades | Return | MAR | Max DD | Sharpe | Worst day |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | Baseline 30d uptrend | 2,367 | +26.64% | 7.33 | -1.13% | 3.37 | -0.93% |
| Bybit | Gate off | 4,030 | +26.53% | 2.33 | -3.52% | 2.25 | -1.98% |
| Bybit | 25d uptrend | 2,261 | +20.72% | 4.29 | -1.50% | 2.85 | -0.93% |
| Bybit | 60d uptrend | 2,357 | +23.27% | 4.27 | -1.69% | 2.81 | -0.93% |
| Binance | Baseline 30d uptrend | 2,152 | +18.84% | 5.72 | -1.02% | 2.63 | -0.63% |
| Binance | Gate off | 3,799 | +12.99% | 0.86 | -4.64% | 1.09 | -2.22% |
| Binance | 25d uptrend | 2,043 | +15.79% | 3.89 | -1.26% | 2.34 | -0.63% |
| Binance | 60d uptrend | 2,140 | +20.25% | 4.84 | -1.29% | 2.69 | -0.78% |

The gate-off result roughly doubled component trades and damaged MAR/drawdown.
This does not prove the 30d gate is a universal parameter. It does prove that
removing the current gate is a bad current-control change.

## BTC-Risk Tail Sizing And Skip Logic

Hard-skipping the 35% BTC-risk tail state was rejected by the preregistered
two-venue rule.

| Venue | Arm | Trades | Return | MAR | Max DD | Sharpe |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | Baseline 35% sizing | 2,367 | +26.64% | 7.33 | -1.13% | 3.37 |
| Bybit | Hard skip <=35% | 2,189 | +26.68% | 7.08 | -1.17% | 3.26 |
| Binance | Baseline 35% sizing | 2,152 | +18.84% | 5.72 | -1.02% | 2.63 |
| Binance | Hard skip <=35% | 1,990 | +20.49% | 7.15 | -0.89% | 2.83 |

The hard skip improves Binance but slightly worsens Bybit MAR and drawdown. The
current decision is to keep 35% sizing rather than hard-skip unless forward OOS
contradicts this.

New open falsifier: `skip_btc_tail_035_btc_gate_off` has been registered but
has not produced a completed full-replay result. It is not evidence yet.

## Scale-In Research

Conditional add-ons after MAE were a mechanism lead but failed portfolio replay.

| Venue | Arm | Parent trades | Child trades | Child fill rate | Return | MAR | Max DD | Return delta | MAR delta |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | Baseline | 2,367 | 0 | 0.00% | +26.64% | 7.33 | -1.13% | 0.00% | 0.00 |
| Bybit | MAE 5%, add 25% | 2,367 | 1,274 | 53.82% | +31.17% | 6.75 | -1.43% | +4.53% | -0.58 |
| Bybit | MAE 5%, add 50% | 2,367 | 1,274 | 53.82% | +35.83% | 6.39 | -1.74% | +9.19% | -0.94 |
| Bybit | MAE 10%, add 50% | 2,367 | 764 | 32.28% | +33.11% | 6.27 | -1.64% | +6.47% | -1.06 |
| Binance | Baseline | 2,152 | 0 | 0.00% | +18.84% | 5.72 | -1.02% | 0.00% | 0.00 |
| Binance | MAE 5%, add 25% | 2,152 | 1,123 | 52.18% | +21.66% | 5.34 | -1.25% | +2.82% | -0.38 |
| Binance | MAE 5%, add 50% | 2,152 | 1,123 | 52.18% | +24.53% | 4.96 | -1.53% | +5.69% | -0.76 |
| Binance | MAE 10%, add 50% | 2,152 | 682 | 31.69% | +23.54% | 5.36 | -1.36% | +4.70% | -0.36 |

Return improved, but every tested arm worsened MAR and drawdown. This is
leverage-like behavior, not a deployment improvement.

## Signal Invalidation Research

Sparse candidate-tape invalidation exits were negative or zero-hit. The least
harmful active arm was still harmful.

| Venue | Rule | Invalidations | Rate | Original net | Scenario net | Delta |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | `candidate_pressure_3h_score99` | 211 | 8.91% | +20.89% | +17.85% | -3.04% |
| Binance | `candidate_pressure_3h_score99` | 127 | 5.90% | +14.69% | +12.87% | -1.83% |
| Bybit | `btc_trend_reject_3h` | 0 | 0.00% | +20.89% | +20.89% | 0.00% |
| Binance | `btc_trend_reject_3h` | 0 | 0.00% | +14.69% | +14.69% | 0.00% |

Hourly state coverage is not sufficient for a live invalidation model:

| Venue | Trades | State rows | Candidate-state coverage | OI coverage | Funding coverage | Spread/depth |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | 2,367 | 48,447 | 2.45% | 67.55% | 100.00% | 0.00% |
| Binance | 2,152 | 44,416 | 2.25% | 7.12% | 99.74% | 0.00% |

Conclusion: do not add a live invalidation exit without a full hourly state
panel and a full component+hedge replay.

## Cost And Funding Sensitivity

Component-ledger stress:

| Venue | Scenario | Net PnL | Profit factor | Worst trade |
| --- | --- | ---: | ---: | ---: |
| Bybit | Baseline | +20.89% | 1.82 | -0.383% |
| Bybit | +10 bps extra cost | +19.90% | 1.77 | -0.383% |
| Bybit | Funding 2x | +18.11% | 1.67 | -0.430% |
| Bybit | Funding excluded | +23.66% | 1.98 | -0.336% |
| Binance | Baseline | +14.69% | 1.56 | -0.334% |
| Binance | +10 bps extra cost | +13.76% | 1.52 | -0.335% |
| Binance | Funding 2x | +12.79% | 1.47 | -0.345% |
| Binance | Funding excluded | +16.60% | 1.66 | -0.323% |

The book survives moderate cost/funding stresses internally, but cost evidence
is not complete until forward paper/demo measures fill latency, maker/taker mix,
fees, funding, and PostOnly cancel behavior.

## Tail And Cluster Risk

### Synthetic Active-Book Shocks

Worst active placements:

| Venue | Scenario | Hit symbols | Hit notional | Net loss | Post-event DD | Recovery days |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| Bybit | one coin +100% | CVCUSDT | 3.12% | 3.12% | -3.04% | 168.6 |
| Bybit | exchange down one coin +100% | CVCUSDT | 3.12% | 3.43% | -3.35% | 178.6 |
| Bybit | risk daemon down one coin +100% | CVCUSDT | 3.12% | 3.74% | -3.65% | 181.6 |
| Bybit | three coins +50% | JSTUSDT,SFPUSDT,OPUSDT | 6.56% | 3.28% | -3.97% | 258.6 |
| Binance | one coin +100% | DARUSDT | 3.15% | 3.15% | -3.48% | 115.6 |
| Binance | exchange down one coin +100% | DARUSDT | 3.15% | 3.46% | -3.78% | 162.6 |
| Binance | risk daemon down one coin +100% | DARUSDT | 3.15% | 3.78% | -4.09% | 238.6 |
| Binance | three coins +50% | DARUSDT,UMAUSDT,API3USDT | 6.97% | 3.49% | -3.63% | 215.3 |

The strategy survives these current-size synthetic shocks, but recovery can be
slow and the diagnostic is not an exchange liquidation engine.

### Dynamic 5m Tail Overlay

Dynamic overlay with actual post-event 5m paths found no maintenance-proxy
liquidations across the 42 tested rows. Worst peak/flatten losses were around
3.3% to 3.7% in the listed worst-active cases. This supports tiny-size
observation. It does not justify size increases.

### Cluster Bootstrap

Cluster bootstrap summary:

| Venue | Scenario | Median final return | p05 final return | p01 final return | p(DD >=10%) | Account impairment |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | Cluster bootstrap | +23.24% | +16.31% | +13.11% | 0.00% | 0.00% |
| Bybit | One worst +100% shock | +19.47% | +12.60% | +9.93% | 0.00% | 0.00% |
| Bybit | Worst 5% clusters at 3x | -6.42% | -13.52% | -15.96% | 33.70% | 0.00% |
| Binance | Cluster bootstrap | +15.73% | +9.52% | +7.10% | 0.00% | 0.00% |
| Binance | One worst +100% shock | +12.23% | +6.16% | +3.84% | 0.00% | 0.00% |
| Binance | Worst 5% clusters at 3x | -10.23% | -16.60% | -19.40% | 66.18% | 0.00% |

The fragility case is not a single current-size shock. It is repeated bad
clusters. That argues for heat caps and account drawdown brakes.

### Disaster-Loss Sizing

The survival overlays are not enough. Disaster-loss sizing says current
notional is too high for strict per-trade loss budgets under a +100% adverse
move.

| Venue | Scenario | Per-trade loss budget | Safe notional | Trades over budget |
| --- | --- | ---: | ---: | ---: |
| Bybit | fixed +100% | 0.10% equity | 0.10% equity | 97.34% |
| Bybit | fixed +100% | 0.25% equity | 0.25% equity | 77.14% |
| Binance | fixed +100% | 0.10% equity | 0.10% equity | 97.44% |
| Binance | fixed +100% | 0.25% equity | 0.25% equity | 78.53% |

Conclusion: before any size increase, the book needs explicit loss-at-disaster
caps per trade and per cluster.

## Overfit And Inference Risk

The frozen full-replay surface is inference-fragile:

| Venue | Variants | PBO | Median OOS rank | Baseline DSR prob | Best Sharpe variant | Best DSR prob | Verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| Bybit | 21 | 41.43% | 50.00% | 23.17% | `mae05_add25` | 24.03% | fragile |
| Binance | 21 | 35.71% | 71.43% | 20.08% | `skip_btc_tail_035` | 28.77% | fragile |

The variants with the best internal Sharpe were already rejected by their own
two-venue MAR/drawdown rules. This is a warning against selecting parameters
from internal rankings.

## Operational Safety And Forward Readiness

Current live-safety plumbing is materially better than the original research
book:

- Entry risk-health gate.
- Private snapshot error block.
- Private execution WS stale block.
- Continuous ledger vs venue position consistency block.
- Recent continuous-order exchange-only position block.
- Non-hedge unprotected-position block.
- Unprotected-position age telemetry.
- Explicit trade lifecycle transition table.
- Append-only risk event log.
- Append-only lifecycle event log.
- Stop/take-profit repair audit log in `ws_risk`.
- Portfolio heat cap.
- Account drawdown kill-switch.

Forward readiness remains `False` because there are no trades:

| Metric | Current value |
| --- | ---: |
| Paper cycles after v2 clock | 121 |
| Demo cycles after v2 clock | 123 |
| Paper operational audit | Pass |
| Demo operational audit | Pass |
| Paper trades | 0 |
| Demo trades | 0 |
| Paired paper/demo trades | 0 |
| Entry-risk blocks | 0 |
| Order failures | 0 |
| Unprotected-position seconds | 0 |
| Portfolio-heat clamps | 0 |
| Account-drawdown kill-switch activations | 0 |

This is clean cycle evidence, not execution-performance evidence.

Not yet measurable:

- Fill rate.
- Fill latency.
- PostOnly cancel rate.
- Fees.
- Funding in forward.
- Maker/taker mix.
- Stop placement latency.
- Stop repair count.
- Paper/demo slippage.

## Rejected Changes

| Change | Verdict | Reason |
| --- | --- | --- |
| Add 1h/2h entry delay | Reject | Lower return/MAR on both venues or weaker portfolio quality |
| Adverse-limit +1% entry | Reject | Signal-level hint failed full component+hedge replay |
| Fixed 20/40/80% stops | Reject | All trailed no-stop baseline; 20% stop badly damaged book |
| BTC gate off | Reject | Large MAR/DD degradation, especially Binance |
| BTC lookback retune | Reject | Non-30d lookbacks did not beat baseline MAR on both venues |
| Hard skip BTC-risk <=35% | Reject for deployment | Binance improved but Bybit MAR/DD worsened |
| Scale-in after MAE | Reject | Return up, MAR/DD down |
| Sparse candidate-pressure invalidation | Reject | Reduced component net on both venues |
| Trust internal Sharpe ranking | Reject | DSR/PBO flags fragile variant surface |

## Current Recommended Design

For demo/paper only:

```text
Signal:
  short crowded high-turnover hourly pop candidates
  within low residual-momentum symbols
  using max_ret168 as composite feature

Entries:
  near-immediate after closed-bar confirmation
  no TWAP
  no added delay
  no adverse-limit wait

Exits:
  12% component take-profit
  24h max hold
  no tactical fixed stop

Regime:
  keep 30d BTC uptrend gate
  keep BTC-vol hedge regime

Sizing:
  inverse-vol per name
  current tiny-size observation
  keep CTRL_BTC_RISK_70_90_35 sizing overlay
  do not replace it with hard skip without forward evidence

Risk:
  enforce portfolio heat cap
  enforce account drawdown kill-switch
  enforce reconciliation/private-state health
  add explicit disaster-loss budgets before any size increase
```

## What Would Change The View

More bullish:

- Forward paper/demo reaches at least the warning threshold of paired trades
  with small paper/demo drift.
- Fill latency and maker/taker mix are benign.
- Funding/costs in forward match or improve on modeled assumptions.
- Current tiny-size observations show no unprotected-position or lifecycle
  anomalies.
- Same mechanism remains positive across fresh forward trade clusters.

More bearish:

- Forward paired trades materially lag paper returns.
- PostOnly sniper fills mostly losing continuation cases.
- MAE tail clusters repeat faster than the internal cluster bootstrap implies.
- BTC gate or BTC-risk sizing creates live/execution mismatch.
- Private-state or lifecycle gates block often enough to invalidate the
  executable signal.
- Spread/depth data, when complete, shows entries are not capacity-realistic.

## Final Recommendation

Do not promote this strategy to mainnet and do not increase size from internal
replay evidence.

The book has a plausible and internally repeatable short-fade mechanism, but it
is adverse-tail exposed and inference-fragile. The correct posture is continued
tiny-size demo/paper observation with strict operational risk gates and
disaster-loss budgeting.

The central unresolved question is not whether the backtest can be made prettier.
It is whether the live paper/demo path can execute the same edge without
slippage, state mismatch, unprotected exposure, or repeated adverse-tail
clusters.

## Artifact Map

Primary:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/run_metadata.json`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/reports/final_research_report.md`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/bybit/continuous_equity_summary.json`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/binance/continuous_equity_summary.json`

Core tables:

- `tables/trades_enriched.parquet`
- `tables/signal_table.parquet`
- `tables/forward_path_by_signal.parquet`
- `tables/trade_path_metrics.parquet`
- `tables/pnl_by_component.csv`
- `tables/pnl_by_year.csv`
- `tables/mae_conditional_recovery.csv`
- `tables/path_label_summary.csv`
- `tables/timing_portfolio_replay.csv`
- `tables/stop_portfolio_replay.csv`
- `tables/regime_portfolio_replay.csv`
- `tables/skip_portfolio_replay.csv`
- `tables/scale_in_portfolio_replay.csv`
- `tables/signal_invalidation_summary.csv`
- `tables/signal_invalidation_state_panel_summary.csv`
- `tables/cost_funding_scenarios.csv`
- `tables/hedge_attribution.csv`
- `tables/synthetic_squeeze_survival.csv`
- `tables/dynamic_liquidation_outage.csv`
- `tables/cluster_risk_of_ruin.csv`
- `tables/disaster_sizing_summary.csv`
- `tables/pbo_cscv_summary.csv`

Receipts:

- `docs/preregistration/2026-06-27-continuous-fade-validation-baseline.md`
- `docs/preregistration/2026-06-28-continuous-fade-btc-risk-tail-skip.md`
- `docs/preregistration/2026-06-28-continuous-fade-cluster-risk-of-ruin.md`
- `docs/preregistration/2026-06-28-continuous-fade-disaster-loss-sizing.md`
- `docs/preregistration/2026-06-28-continuous-fade-dsr-pbo.md`
- `docs/preregistration/2026-06-28-continuous-fade-dynamic-liquidation-outage.md`
- `docs/preregistration/2026-06-28-continuous-fade-scale-in-portfolio-replay.md`
- `docs/preregistration/2026-06-28-continuous-fade-signal-invalidation.md`
- `docs/preregistration/2026-06-28-continuous-fade-synthetic-squeeze-survival.md`
- `docs/preregistration/2026-06-28-continuous-forward-readiness.md`
