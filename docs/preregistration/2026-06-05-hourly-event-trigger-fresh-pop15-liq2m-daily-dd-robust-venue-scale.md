# Pre-registration: Fresh Pop15 2m Daily-DD Robust Venue Scales

**Date:** 2026-06-05
**Author:** Codex
**Stage:** accepted

## What's changing

Define a stress-margin venue-scale variant for the accepted `fresh_pop15` 2m
liquidity + daily drawdown gated overlay:

- Bybit scale: `1.65`
- Binance scale: `1.25`

## Hypothesis

The max-return venue-scale allocation (`bybit=1.75`, `binance=1.50`) passes, but
its 2x-cost drawdown ratios sit close to the `1.10` gate on both venues. A
slightly lower, asymmetric risk budget should retain most of the combined-book
benefit while adding meaningful stress margin.

## Predicted direction + magnitude

- Both venues should still beat the scale `1.0` daily-DD-gated variant on
  combined return and MAR.
- Both venues should have 2x-cost drawdown ratios below `1.08`.
- Bybit should remain above global scale `1.5` on return/MAR because its scale
  stays above `1.5`.
- Binance should give up some return/MAR versus scale `1.5`, but keep better
  return/MAR than scale `1.0`.
- Failure mode if hypothesis wrong: the lower Binance allocation gives up too
  much return without materially improving drawdown, making the variant
  dominated by the max-return allocation.

## Roots that will be touched

- [ ] bybit_full_pit (per-venue working dataset)
- [ ] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper

This run recombines existing execution artifacts and rebuilds filtered overlay
MTM from saved trade ledgers; it does not rerun per-venue signal generation.

## Frozen grid

- Source execution artifact:
  `C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05`
- Cell:
  `q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m`
- Overlay construction: `short_only`
- Daily drawdown entry gate: prior closed daily-book drawdown `>= -0.10`
- Venue scales: `bybit=1.65,binance=1.25`
- Costs: existing continuous execution model, funding and impact included
- Stress: repeat combined-book check with 2x overlay trading costs.

## Decision rule (a priori)

Accept the robust venue scales only if all of the following are true:

- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- the 2x overlay-cost stress still passes the same combined-book MAR/total/DD
  gates,
- the 2x overlay-cost drawdown ratio is `<= 1.08` on both venues,
- compared with the scale `1.0` daily-DD-gated variant, combined return and MAR
  improve on both venues.

Passing is research evidence only and is not real-money promotion.

## Run command

```bash
.\.venv\Scripts\python.exe scripts\combine_daily_continuous_overlay.py --execution-root $HOME\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05 --cell-id q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m --overlay-construction short_only --scale 1.0 --venue-scales bybit=1.65,binance=1.25 --daily-dd-entry-min -0.10 --pre-registration docs/preregistration/2026-06-05-hourly-event-trigger-fresh-pop15-liq2m-daily-dd-robust-venue-scale.md --out $HOME\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_robust_venue_scale_2026-06-05
```

## Post-run results

Combined-book roots:

- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_robust_venue_scale_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_robust_venue_scale_2026-06-05_cost200`

Scale 1.0 control:
`C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_2026-06-05`

Max-return venue-scale reference:
`C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_venue_scale_2026-06-05`

Base combined-book result:

| Venue | Variant | Scale | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | scale 1.0 | 1.00 | 116.50% | 5.57 | 3.40 | 0.958 | yes |
| Binance | scale 1.0 | 1.00 | 24.59% | 0.72 | 1.49 | 1.034 | yes |
| Bybit | robust venue scale | 1.65 | 141.89% | 6.16 | 3.62 | 1.055 | yes |
| Binance | robust venue scale | 1.25 | 28.93% | 0.84 | 1.64 | 1.048 | yes |

At 2x overlay costs, the robust venue scales still pass the stricter stress
margin:

| Venue | Scale | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 1.65 | 137.48% | 5.85 | 3.55 | 1.076 | yes |
| Binance | 1.25 | 27.31% | 0.77 | 1.56 | 1.077 | yes |

## Verdict

Accepted as the stress-margin allocation variant. It beats scale `1.0` on
combined return and MAR on both venues, while keeping both 2x-cost drawdown
ratios under `1.08`. It is not the max-return allocation; it is the cleaner
risk-budget version for research comparison. No real-money promotion is implied.
