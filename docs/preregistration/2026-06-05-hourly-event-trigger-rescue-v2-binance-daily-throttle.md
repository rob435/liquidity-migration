# Pre-registration: Rescue V2 Binance Daily Throttle

**Date:** 2026-06-05
**Author:** Codex
**Stage:** accepted

## What's changing

Add a causal portfolio throttle to the accepted rescue V2 allocation:

- Bybit daily sleeve: unchanged.
- Binance daily sleeve: scale daily-sleeve returns to `0.0` when the deployed
  daily short book's prior closed drawdown is `<= -5%`.
- Hourly overlay: unchanged from rescue V2.

## Hypothesis

Temporal decomposition showed Binance 2026 weakness is driven by the deployed
daily short sleeve, while the hourly overlay remains positive. A causal
daily-sleeve throttle should reduce the Binance drawdown drag without changing
the hourly `fresh_pop15` signal.

## Predicted direction + magnitude

- Binance full-window combined return, MAR, and Sharpe should improve versus
  rescue V2.
- Binance 2026 combined return should improve materially.
- Bybit should remain numerically unchanged.
- Both venues should still pass base and 2x overlay-cost combined-book gates.
- Failure mode if hypothesis wrong: throttled Binance daily days were recovery
  winners, reducing return or creating worse path dependency.

## Roots that will be touched

- [ ] bybit_full_pit (per-venue working dataset)
- [ ] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper

This run recombines existing execution artifacts and rebuilt overlay MTM
artifacts; it does not rerun per-venue signal generation.

## Frozen grid

- Source execution artifact:
  `C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05`
- Cell:
  `q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m`
- Overlay construction: `short_only`
- Daily drawdown entry gate: prior closed daily-book drawdown `>= -0.10`
- Overlay rescue trigger: prior closed daily-book drawdown `<= -0.05`
- Base overlay scales: `bybit=1.65,binance=1.25`
- Rescue overlay scales: `bybit=1.80,binance=2.00`
- Daily throttle trigger: prior closed daily-book drawdown `<= -0.05`
- Daily throttle scales: `binance=0.0`; Bybit omitted/unchanged
- Stress: repeat combined-book check with 2x overlay trading costs.

## Decision rule (a priori)

Accept the Binance daily throttle only if all of the following are true:

- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- the 2x overlay-cost stress still passes the same combined-book MAR/total/DD
  gates,
- compared with rescue V2, Binance combined return, MAR, Sharpe, and 2026
  combined return improve,
- Bybit combined return, MAR, and drawdown ratio are unchanged within numerical
  tolerance versus rescue V2.

Passing is research evidence only and is not real-money promotion.

## Run command

```bash
.\.venv\Scripts\python.exe scripts\combine_daily_continuous_overlay.py --execution-root $HOME\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05 --cell-id q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m --overlay-construction short_only --scale 1.0 --venue-scales bybit=1.65,binance=1.25 --daily-dd-entry-min -0.10 --daily-dd-rescue-trigger -0.05 --venue-rescue-scales bybit=1.80,binance=2.00 --daily-dd-throttle-trigger -0.05 --venue-daily-throttle-scales binance=0.0 --pre-registration docs/preregistration/2026-06-05-hourly-event-trigger-rescue-v2-binance-daily-throttle.md --out $HOME\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05
```

## Post-run results

Combined-book roots:

- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05_cost200`

Rescue V2 control:
`C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_rescue_scale_v2_2026-06-05`

Base combined-book result:

| Venue | Daily throttled days | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 0 | 142.16% | 6.16 | 3.62 | 1.057 | yes |
| Binance | 19 | 34.44% | 1.12 | 1.87 | 0.928 | yes |

At 2x overlay costs:

| Venue | Daily throttled days | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 0 | 137.71% | 5.85 | 3.55 | 1.079 | yes |
| Binance | 19 | 32.54% | 1.04 | 1.78 | 0.947 | yes |

Comparison to rescue V2 on Binance:

| Variant | Return | MAR | Sharpe | 2026 return | DD ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Rescue V2 | 31.16% | 0.92 | 1.64 | -4.61% | 1.021 |
| Rescue V2 + daily throttle | 34.44% | 1.12 | 1.87 | -1.18% | 0.928 |

## Verdict

Accepted as the best current Binance recovery/defense variant. The throttle is
causal, affects only 19 Binance daily-sleeve return days, leaves Bybit
unchanged, and improves Binance full-window return, MAR, Sharpe, drawdown ratio,
and 2026 return. It does not make Binance 2026 positive; it materially reduces
the daily-sleeve drawdown drag. No real-money promotion is implied.
