# Pre-registration: Fresh Pop15 Liquidity 2m Gate

**Date:** 2026-06-05
**Author:** Codex
**Stage:** accepted

## What's changing

Raise the sparse hourly `fresh_pop15` MAX short overlay liquidity gate from
hourly `turnover_quote >= 1,000,000` to `>= 2,000,000`.

## Hypothesis

The remaining churn is not mostly same-hour clustering or short-gap repeated
symbols. It is many small catalyst names. A stricter liquidity gate should keep
the liquid migration/fade events, reduce low-quality tails, and improve
execution robustness without changing the event definition.

## Predicted direction + magnitude

- Trade count: lower than the 1m `fresh_pop15` control on both venues, with a
  materially larger reduction on Bybit.
- Standalone short-only return: remains positive on both venues.
- Combined-book quality: can be lower or higher than the 1m control, but must
  still beat the deployed daily book on MAR and total return.
- Failure mode if hypothesis wrong: the 2m gate removes too much of the
  idiosyncratic fade and leaves a lower-return overlay that no longer clears the
  combined-book gate.

## Roots that will be touched

- [x] bybit_full_pit (per-venue working dataset)
- [x] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper

## Frozen grid

- Venues: Bybit, Binance
- Feature set: `max_ret168`
- Residual-momentum quantile: `0.25`
- Liquidity gate: hourly `turnover_quote >= 2,000,000`
- BTC trend gate: `uptrend`
- Side/construction: short-only D9
- Entry event trigger: `fresh_pop15`
- Entry delay: `1` hour after the deciding closed bar
- Hold: `24h`
- Exit mode: `fixed`
- Costs: existing continuous execution model, funding and impact included
- Combined-book check: add short-only overlay at scale `1.0` to deployed daily
  short baseline.
- Stress: repeat combined-book check with 2x overlay trading costs.

## Decision rule (a priori)

Accept the 2m liquidity gate only if all of the following are true:

- standalone short-only overlay has positive MTM total return on both venues,
- trade count is lower than the 1m `fresh_pop15` control on both venues,
- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- the 2x overlay-cost stress still passes the same combined-book MAR/total/DD
  gates.

If the 2m gate passes but has worse combined return, MAR, and Sharpe than the 1m
control on both venues, keep 1m as the primary candidate and classify 2m as a
capacity/turnover variant only. Passing is research evidence only and is not
real-money promotion.

## Run command

```bash
.\.venv\Scripts\python.exe scripts\continuous_causal_rmom_vs_daily.py --venues bybit,binance --rmom-quantiles 0.25 --feature-set max_ret168 --liq-turnover-mins 2000000 --hold-hours 24 --exit-modes fixed --btc-trend-gates uptrend --entry-event-triggers fresh_pop15 --pre-registration docs/preregistration/2026-06-05-hourly-event-trigger-fresh-pop15-liq2m.md --out $HOME\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05
```

## Post-run results

Run root:
`C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05`

Combined-book roots:

- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_2026-06-05_cost200`

Exploratory scout used only to justify running this check:
`C:\Users\user\SHARED_DATA\continuous_event_trigger_liq2m_scout_2026-06-05.csv`

Standalone short-only overlay versus 1m control:

| Venue | Liquidity gate | Trades | Total return | MAR | Sharpe-like | Max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | 1m control | 301 | 17.76% | 1.49 | 2.61 | -3.92% |
| Binance | 1m control | 267 | 13.90% | 1.46 | 2.53 | -3.35% |
| Bybit | 2m | 258 | 17.64% | 2.26 | 3.00 | -2.56% |
| Binance | 2m | 263 | 13.86% | 1.45 | 2.48 | -3.35% |

Combined daily+short-only overlay:

| Venue | Liquidity gate | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 1m control | 116.74% | 5.96 | 3.31 | 0.898 | yes |
| Binance | 1m control | 24.21% | 0.72 | 1.46 | 1.013 | yes |
| Bybit | 2m | 116.50% | 5.57 | 3.40 | 0.958 | yes |
| Binance | 2m | 24.16% | 0.69 | 1.46 | 1.060 | yes |

At 2x overlay costs, the 2m variant still passes:

| Venue | Combined return | Combined MAR | DD ratio | Pass |
| --- | ---: | ---: | ---: | --- |
| Bybit | 114.10% | 5.38 | 0.971 | yes |
| Binance | 22.88% | 0.64 | 1.085 | yes |

## Verdict

Accepted as a cleaner liquidity/capacity variant, not a full replacement for
the 1m primary on raw combined MAR/return. The 2m gate passes every
pre-registered base and 2x-cost gate while cutting Bybit trades from 301 to 258
and improving Bybit standalone drawdown/MAR materially. Binance is nearly
unchanged in trade count and standalone quality. Keep 1m `fresh_pop15` as the
highest raw combined-return candidate; use 2m when the objective is lower
turnover, cleaner liquidity, and better Bybit standalone risk.
