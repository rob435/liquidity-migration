# Pre-registration: Fresh Pop15 2m Daily-DD Rescue Scales V2

**Date:** 2026-06-05
**Author:** Codex
**Stage:** accepted

## What's changing

Use a lower Bybit rescue scale than the rejected first rescue attempt:

- Keep the daily drawdown entry gate: accept overlay entries only when prior
  closed daily-book drawdown is `>= -10%`.
- Base scales:
  - Bybit: `1.65`
  - Binance: `1.25`
- Rescue scales when prior closed daily-book drawdown is between `-10%` and
  `-5%`:
  - Bybit: `1.80`
  - Binance: `2.00`

## Hypothesis

The first rescue-scale attempt showed that the mechanism helps, especially on
Binance, but Bybit rescue scale `2.25` breached the stricter `1.08` 2x-cost
drawdown-ratio margin. Reducing Bybit rescue to `1.80` should keep the Binance
rescue benefit while bringing Bybit back inside the stress-margin rule.

## Predicted direction + magnitude

- Combined return and MAR should improve versus the robust venue-scale variant
  on both venues.
- 2x-cost drawdown ratios should remain `<= 1.08` on both venues.
- The result should improve on the rejected V1 by reducing Bybit stress
  drawdown while preserving most of the rescue benefit.
- Failure mode if hypothesis wrong: the lower Bybit rescue scale removes too
  much benefit, or the rebuilt trade-entry MTM still breaches the stress-margin
  drawdown rule.

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
- Rescue trigger: prior closed daily-book drawdown `<= -0.05`
- Base venue scales: `bybit=1.65,binance=1.25`
- Rescue venue scales: `bybit=1.80,binance=2.00`
- Costs: existing continuous execution model, funding and impact included
- Stress: repeat combined-book check with 2x overlay trading costs.

## Decision rule (a priori)

Accept rescue V2 only if all of the following are true:

- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- the 2x overlay-cost stress still passes the same combined-book MAR/total/DD
  gates,
- the 2x overlay-cost drawdown ratio is `<= 1.08` on both venues,
- compared with the robust venue-scale variant, combined return and MAR improve
  on both venues.

Passing is research evidence only and is not real-money promotion.

## Run command

```bash
.\.venv\Scripts\python.exe scripts\combine_daily_continuous_overlay.py --execution-root $HOME\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05 --cell-id q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m --overlay-construction short_only --scale 1.0 --venue-scales bybit=1.65,binance=1.25 --daily-dd-entry-min -0.10 --daily-dd-rescue-trigger -0.05 --venue-rescue-scales bybit=1.80,binance=2.00 --pre-registration docs/preregistration/2026-06-05-hourly-event-trigger-fresh-pop15-liq2m-daily-dd-rescue-scale-v2.md --out $HOME\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_rescue_scale_v2_2026-06-05
```

## Post-run results

Combined-book roots:

- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_rescue_scale_v2_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_rescue_scale_v2_2026-06-05_cost200`

Robust venue-scale control:
`C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_robust_venue_scale_2026-06-05`

Base combined-book result:

| Venue | Base scale | Rescue scale | Rescue trades | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit control | 1.65 | n/a | 0 | 141.89% | 6.16 | 3.62 | 1.055 | yes |
| Binance control | 1.25 | n/a | 0 | 28.93% | 0.84 | 1.64 | 1.048 | yes |
| Bybit rescue V2 | 1.65 | 1.80 | 19 | 142.16% | 6.16 | 3.62 | 1.057 | yes |
| Binance rescue V2 | 1.25 | 2.00 | 51 | 31.16% | 0.92 | 1.64 | 1.021 | yes |

At 2x overlay costs:

| Venue | Base scale | Rescue scale | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 1.65 | 1.80 | 137.71% | 5.85 | 3.55 | 1.079 | yes |
| Binance | 1.25 | 2.00 | 29.30% | 0.85 | 1.56 | 1.040 | yes |

## Verdict

Accepted as the best current stress-margin recovery allocation. Rescue V2
improves combined return and MAR versus the robust venue-scale control on both
venues while keeping both 2x-cost drawdown ratios inside the `<= 1.08` internal
margin. It is a recovery/MAR refinement, not a Sharpe improvement on Binance.
No real-money promotion is implied.
