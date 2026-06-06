# Pre-registration: Fresh Pop15 2m Daily Drawdown Gate

**Date:** 2026-06-05
**Author:** Codex
**Stage:** accepted

## What's changing

Add a combined-book entry gate to the accepted `fresh_pop15` 2m liquidity
variant: skip new overlay trades when the deployed daily short book's prior
closed drawdown is worse than `-10%`.

## Hypothesis

The rejected adverse-exit breaker showed that standalone overlay improvement is
not enough; the failure was combined-book timing. A causal daily-book pressure
gate should avoid adding the hourly short overlay when the deployed daily short
book is already in a deep drawdown state, reducing combined drawdown without
changing the overlay's event definition.

## Predicted direction + magnitude

- Trade count: unchanged or slightly lower on Bybit; lower on Binance.
- Standalone overlay return: may be slightly lower because the filter skips
  trades, but should remain positive on both venues.
- Combined-book drawdown ratio: should improve or stay flat versus the 2m
  control on both venues.
- Failure mode if hypothesis wrong: the skipped trades were rebound/fade winners
  that helped recover the daily-book drawdown, reducing MAR/return or increasing
  drawdown via worse path dependency.

## Roots that will be touched

- [x] bybit_full_pit (per-venue working dataset)
- [x] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper

## Frozen grid

- Venues: Bybit, Binance
- Source execution artifact:
  `C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05`
- Cell:
  `q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m`
- Overlay construction: `short_only`
- Overlay scale: `1.0`
- Daily drawdown entry gate: require prior closed daily-book drawdown `>= -0.10`
- Costs: existing continuous execution model, funding and impact included
- Stress: repeat combined-book check with 2x overlay trading costs.

## Decision rule (a priori)

Accept the daily drawdown gate as a combined-book risk variant only if all of
the following are true:

- standalone filtered overlay has positive MTM total return on both venues,
- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- the 2x overlay-cost stress still passes the same combined-book MAR/total/DD
  gates,
- compared with the ungated 2m control, combined drawdown ratio does not worsen
  on either venue and either combined MAR or combined return improves on at
  least one venue.

If it passes but has lower raw return than the ungated 2m control, classify it
as a risk-gated variant, not the primary candidate. Passing is research evidence
only and is not real-money promotion.

## Run command

```bash
.\.venv\Scripts\python.exe scripts\combine_daily_continuous_overlay.py --execution-root $HOME\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05 --cell-id q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m --overlay-construction short_only --scale 1.0 --daily-dd-entry-min -0.10 --pre-registration docs/preregistration/2026-06-05-hourly-event-trigger-fresh-pop15-liq2m-daily-dd-gate.md --out $HOME\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_2026-06-05
```

## Post-run results

Combined-book roots:

- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_2026-06-05_cost200`

Ungated 2m control:
`C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_2026-06-05`

Base combined-book result:

| Venue | Variant | Overlay trades | Skipped | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 2m control | 258 | 0 | 116.50% | 5.57 | 3.40 | 0.958 | yes |
| Binance | 2m control | 263 | 0 | 24.16% | 0.69 | 1.46 | 1.060 | yes |
| Bybit | daily DD gate | 258 | 0 | 116.50% | 5.57 | 3.40 | 0.958 | yes |
| Binance | daily DD gate | 258 | 5 | 24.59% | 0.72 | 1.49 | 1.034 | yes |

At 2x overlay costs, the gate still passes:

| Venue | Combined return | Combined MAR | DD ratio | Pass |
| --- | ---: | ---: | ---: | --- |
| Bybit | 114.10% | 5.38 | 0.971 | yes |
| Binance | 23.34% | 0.67 | 1.057 | yes |

## Verdict

Accepted as a combined-book risk-gated variant. The gate is causal, because it
uses only the deployed daily short book's prior closed drawdown before accepting
new overlay entries. It leaves Bybit unchanged and improves Binance combined
return, MAR, Sharpe, and drawdown ratio while still passing 2x overlay-cost
stress. It is not a real-money promotion; it is a research-stage portfolio gate
for the 2m `fresh_pop15` variant.
