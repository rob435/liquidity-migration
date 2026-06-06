# Pre-registration: Fresh Pop15 Hold Extension

**Date:** 2026-06-05
**Author:** Codex
**Stage:** rejected

## What's changing

Extend the sparse hourly `fresh_pop15` MAX short overlay hold from the accepted
24h control to 36h and 48h.

## Hypothesis

The event is not a catch-the-top signal. It is a confirmed migration/fade
catalyst: after a fresh 15% hourly pop into the MAX residual-momentum tail, the
giveback should continue beyond the first daily close. If that mechanism is
right, a 36h or 48h fixed hold should improve combined-book return or Sharpe
without reintroducing the always-on trade count problem.

## Predicted direction + magnitude

- Standalone short-only total return: positive on both venues and at least
  comparable to the 24h `fresh_pop15` control.
- Combined-book Sharpe or total return: improves versus the 24h control on at
  least one venue without failing either venue.
- Trade count: similar to the 24h control; any change should come only from
  end-of-sample censoring, not from a broader entry gate.
- Failure mode if hypothesis wrong: later hold captures rebound instead of
  continued fade, causing lower combined Sharpe/MAR or drawdown ratio above the
  daily-book tolerance.

## Roots that will be touched

- [x] bybit_full_pit (per-venue working dataset)
- [x] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper

## Frozen grid

- Venues: Bybit, Binance
- Feature set: `max_ret168`
- Residual-momentum quantile: `0.25`
- Liquidity gate: hourly `turnover_quote >= 1,000,000`
- BTC trend gate: `uptrend`
- Side/construction: short-only D9
- Entry event trigger: `fresh_pop15`
- Entry delay: `1` hour after the deciding closed bar
- Hold extensions: `36h`, `48h`
- Control for comparison: prior 24h `fresh_pop15` execution artifact from
  `cont_event_trigger_sparse_pop15_2026-06-05`
- Exit mode: `fixed`
- Costs: existing continuous execution model, funding and impact included
- Combined-book check: add short-only overlay at scale `1.0` to deployed daily
  short baseline.
- Stress: repeat combined-book check with 2x overlay trading costs.

## Decision rule (a priori)

Accept a hold extension only if all of the following are true:

- standalone short-only overlay has positive MTM total return on both venues,
- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- the 2x overlay-cost stress still passes the same combined-book MAR/total/DD
  gates,
- the extension improves combined-book Sharpe or total return versus the 24h
  `fresh_pop15` control on at least one venue without breaking any other gate.

If both 36h and 48h pass, prefer the shorter hold unless 48h materially improves
combined-book return or Sharpe with no drawdown penalty. Passing is research
evidence only and is not real-money promotion.

## Run command

```bash
.\.venv\Scripts\python.exe scripts\continuous_causal_rmom_vs_daily.py --venues bybit,binance --rmom-quantiles 0.25 --feature-set max_ret168 --liq-turnover-mins 1000000 --hold-hours 36,48 --exit-modes fixed --btc-trend-gates uptrend --entry-event-triggers fresh_pop15 --pre-registration docs/preregistration/2026-06-05-hourly-event-trigger-fresh-pop15-hold-extension.md --out $HOME\SHARED_DATA\cont_event_trigger_fresh_pop15_hold_ext_2026-06-05
```

## Post-run results

Run root:
`C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop15_hold_ext_2026-06-05`

Combined-book roots:

- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_h36_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_h48_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_h36_2026-06-05_cost200`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_h48_2026-06-05_cost200`

Standalone short-only overlay:

| Venue | Hold | Trades | Total return | MAR | Max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | 24h control | 301 | 17.76% | 1.49 | -3.92% |
| Binance | 24h control | 267 | 13.90% | 1.46 | -3.35% |
| Bybit | 36h | 301 | 16.28% | 0.99 | -5.38% |
| Binance | 36h | 266 | 6.21% | 0.32 | -6.75% |
| Bybit | 48h | 301 | 16.16% | 0.90 | -5.90% |
| Binance | 48h | 266 | 6.89% | 0.44 | -5.44% |

Combined daily+short-only overlay:

| Venue | Hold | Combined return | Combined MAR | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | --- |
| Bybit | 24h control | 116.74% | 5.96 | 0.898 | yes |
| Binance | 24h control | 24.21% | 0.72 | 1.013 | yes |
| Bybit | 36h | 113.41% | 4.97 | 1.045 | yes |
| Binance | 36h | 14.83% | 0.33 | 1.357 | no |
| Bybit | 48h | 113.07% | 5.65 | 0.917 | yes |
| Binance | 48h | 15.62% | 0.40 | 1.185 | no |

At 2x overlay costs, 36h and 48h still fail the Binance drawdown gate:

| Venue | Hold | Combined return | Combined MAR | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | --- |
| Bybit | 36h | 110.53% | 4.82 | 1.050 | yes |
| Binance | 36h | 13.63% | 0.30 | 1.381 | no |
| Bybit | 48h | 110.20% | 5.47 | 0.923 | yes |
| Binance | 48h | 14.41% | 0.36 | 1.216 | no |

## Verdict

Rejected. The proxy scout correctly suggested that the fade can continue beyond
24h in some cases, but the execution-grade combined-book result is worse than
the 24h control. Binance is the decisive failure: both 36h and 48h increase
combined drawdown beyond the pre-registered `1.10x` limit, and the standalone
short-only overlay also loses MAR versus 24h on both venues. Keep the 24h
`fresh_pop15` sparse event trigger as the current best candidate.
